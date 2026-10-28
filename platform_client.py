"""Local account UI bridge with encrypted, browser-scoped remembered sessions."""
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from platform_archive import platform_url
from remembered_login import SessionFile, LIFETIME, RENEW_INTERVAL
from key_identities import Identities


@dataclass
class Reply:
    status: int
    data: bytes
    mime: str = 'application/json; charset=utf-8'


class PlatformClient:
    def __init__(self, application):
        self.application = application
        self.sessions = {}
        self.accounts = {}
        self.bases = {}
        self.lock = threading.RLock()
        self.saved = SessionFile(application.store.root_directory, 'platform-sessions.dpapi')
        self.identities = Identities(application.store.root_directory)
        self.restore_saved()

    def restore_saved(self):
        value = self.saved.read()
        for row in value.get('accounts', []):
            try:
                browser, base, session = row['browser'], platform_url(row['base']), row['session']
                account = session['account']
                if (not isinstance(browser, str) or len(browser) > 128
                        or account['role'] != 'merchant'
                        or not re.fullmatch(r'teerouter_portal=[A-Za-z0-9_-]{40,128}', session['cookie'])
                        or not isinstance(session['csrf'], str) or not session['csrf']):
                    continue
                # Recheck the cloud session before exposing any account's local data.
                session.update(merchants=set(), restored=True, checked=0)
                self.accounts[(browser, base, account['id'])] = session
                if not account.get('identity_id'):
                    self.identities.for_account(base, account['id'])
                if row.get('active'):
                    self.sessions[(browser, base)] = session
                if row.get('selected_base'):
                    self.bases[browser] = base
            except (KeyError, TypeError, ValueError):
                continue

    def persist(self):
        fields = ('cookie', 'csrf', 'identity', 'account', 'scope', 'expires')
        self.saved.write({'accounts': [
            {'browser': browser, 'base': base,
             'session': {k: session[k] for k in fields},
             'active': self.sessions.get((browser, base)) is session,
             'selected_base': self.bases.get(browser, self.base(browser)) == base}
            for (browser, base, _), session in self.accounts.items()
            ]})

    def restore_browser(self, browser):
        with self.application.state_lock, self.lock:
            session = self.current(browser)
            if not session or time.time() < session.get('retry_at', 0):
                return
            if not session.get('restored') and time.time() - session.get('checked', time.time()) < 300:
                return
            try:
                self.call(browser, 'account', 'GET', 'me', {}, '', self.application.store.enrollment()['id'])
            except (RuntimeError, OSError):
                pass  # Keep encrypted credentials on a temporary outage.
            session['retry_at'] = time.time() + 15

    def base(self, browser):
        return self.bases.get(browser) or platform_url(self.application.store.settings.get('platform_url') or 'https://teerouter.org')

    def current(self, browser):
        """Only this browser's authenticated session can authorize its saved vault."""
        key = (browser, self.base(browser))
        session = self.sessions.get(key)
        if session and session.get('expires', 0) <= time.time():
            session.update(restored=True, merchants=set())
        return session

    def revoke(self, browser, session):
        for key, value in list(self.accounts.items()):
            if key[0] == browser and value is session:
                self.accounts.pop(key, None)
        for key, value in list(self.sessions.items()):
            if key[0] == browser and value is session:
                self.sessions.pop(key, None)
        self.persist()

    def authorized(self, browser, identity=None):
        session = self.current(browser)
        store = self.application.store
        identity = identity or store.enrollment()['id']
        return bool(session and identity == store.enrollment()['id']
                    and self.base(browser) == platform_url(store.settings.get('platform_url'))
                    and identity in session.get('merchants', ())
                    and session.get('identity') == identity and store.credentials()['saved'])

    def require(self, browser, identity=None):
        if not self.authorized(browser, identity):
            raise PermissionError('请登录对应的商户账号')

    def snapshot(self, browser):
        app, store = self.application, self.application.store
        self.restore_browser(browser)
        store = app.store
        with self.lock, store.lock:
            session = self.current(browser)
            allowed = self.authorized(browser)
            base = self.base(browser)
            accounts = [{'id': row['identity_id'], 'identity_id': row['identity_id'],
                         'active': bool(session and session.get('account', {}).get('identity_id') == row['identity_id'])}
                        for row in self.identities.list(base)]
            auth = {'authenticated': bool(session), 'authorized': allowed, 'accounts': accounts,
                    'restoring': bool(session and session.get('restored')),
                    'scope': session.get('scope', '') if session else '',
                    'account': session.get('account') if session else None,
                    'view': session.get('view') if session else None}
            if allowed:
                value = app._api('GET', '/api/state', {})
                value['profiles'] = []
            else:
                value = {'settings': {'platform_url': base}, 'enrollment': {'id': store.enrollment()['id']},
                         'credentials': {'saved': False}, 'identity': {}, 'profiles': [],
                         'connected': False, 'remote': None, 'archive': {}, 'error': ''}
            value['auth'] = auth
            return value

    def set_base(self, browser, data):
        if not isinstance(data, dict) or set(data) != {'platform_url'}:
            raise ValueError('管理平台设置无效')
        with self.lock:
            self.bases[browser] = platform_url(data['platform_url'])
            self.persist()
        return {'saved': True}

    def select(self, browser, data):
        """Switch only to an account authenticated in this browser, never a cached vault."""
        local = self.identities.load(self.base(browser), data.get('account_id'))
        previous = self.accounts.get((browser, self.base(browser), local.get('account_id')))
        if previous and not previous['account'].get('identity_id'):
            return self.key_login(browser, 'bind', previous=previous)
        if (previous and previous['account'].get('identity_id') == local['identity_id']
                and previous.get('expires', 0) > time.time() and not previous.get('restored')):
            identity = previous.get('identity')
            if identity in previous.get('merchants', ()):
                self.application._api('POST', '/api/merchant-profile', {'merchant_id': identity})
            self.sessions[(browser, self.base(browser))] = previous
            self.persist()
            return Reply(200, json.dumps(previous['view'], ensure_ascii=False).encode())
        return self.key_login(browser, 'login' if local.get('account_id') else 'register', local['identity_id'])

    def key_login(self, browser, purpose, identity_id=None, previous=None):
        retained = previous or self.current(browser)
        if retained and purpose != 'bind' and retained['account'].get('identity_id') != identity_id:
            retained = None
        def restore():
            if retained:
                retained.update(merchants=set(), restored=True, checked=0, retry_at=time.time()+15)
                self.sessions[(browser, self.base(browser))] = retained
                self.persist()
        try:
            result = self._key_login(browser, purpose, identity_id, previous)
            if result.status >= 500 or result.status == 429:
                restore()
            return result
        except (httpx.HTTPError, OSError):
            restore()
            raise RuntimeError('无法连接管理平台，身份已保留，请重试') from None
        except RuntimeError:
            restore()
            raise

    def _key_login(self, browser, purpose, identity_id=None, previous=None):
        base = self.base(browser)
        if purpose == 'register':
            local = self.identities.load(base, identity_id) if identity_id else self.identities.create(base)
        elif purpose == 'bind':
            local = self.identities.for_account(base, previous['account']['id'])
        else:
            local = self.identities.load(base, identity_id)
        headers = {'Accept': 'application/json'}
        if purpose == 'bind':
            headers.update(Cookie=previous['cookie'], **{'X-CSRF-Token': previous['csrf']})
        # Switching identities hides the prior account before any network operation.
        if purpose != 'bind':
            self.sessions.pop((browser, base), None)
            self.persist()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            status, value, _ = self.request(client, 'POST', base + '/portal/api/identity/challenge', headers,
                {'role': 'merchant', 'public_key': local['public_key'], 'purpose': purpose})
            if status >= 300:
                return Reply(status, json.dumps(value, ensure_ascii=False).encode())
            proof = self.identities.proof(local, value['challenge'], purpose)
            route = 'identity/bind' if purpose == 'bind' else purpose
            status, value, response_headers = self.request(client, 'POST', base + '/portal/api/' + route, headers, proof)
            if status >= 300:
                return Reply(status, json.dumps(value, ensure_ascii=False).encode())
            account = value.get('account', {})
            if (account.get('role') != 'merchant' or account.get('id') != local['identity_id']
                    or account.get('identity_id') != local['identity_id'] or account.get('public_key') != local['public_key']):
                raise RuntimeError('平台返回的身份与本地密钥不匹配')
            cookie = response_headers.get('set-cookie', '').split(';')[0]
            if not re.fullmatch(r'teerouter_portal=[A-Za-z0-9_-]{40,128}', cookie) or not value.get('csrf'):
                raise RuntimeError('平台登录响应无效')
            self.identities.bind(base, local['identity_id'], account['id'])
            session = {'cookie': cookie, 'csrf': value['csrf'], 'identity': self.application.store.enrollment()['id'],
                       'account': account, 'merchants': set(), 'scope': secrets.token_urlsafe(24),
                       'expires': time.time() + LIFETIME, 'restored': False, 'checked': time.time()}
            self.accounts[(browser, base, account['id'])] = session
            self.sessions[(browser, base)] = session
            try:
                self.bind_account(client, base, session, value)
            except (httpx.HTTPError, ValueError, RuntimeError, OSError):
                session['merchants'] = set()
                value['local_connection_error'] = '商户连接未完成，将自动重试'
            value.pop('csrf', None)
            session['view'] = value
            self.persist()
            return Reply(status, json.dumps(value, ensure_ascii=False).encode())

    @staticmethod
    def request(client, method, url, headers, data=None):
        with client.stream(method, url, headers=headers,
                           **({'json': data or {}} if method != 'GET' else {})) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise RuntimeError('账户响应过大')
                chunks.append(chunk)
            value = json.loads(b''.join(chunks))
            if not isinstance(value, dict):
                raise RuntimeError('平台账户响应无效')
            return response.status_code, value, response.headers

    def refresh_identity(self):
        app = self.application
        with app.state_lock, self.lock:
            store = app.store
            if not store.credentials()['saved']:
                return {}
            base = platform_url(store.settings.get('platform_url') or 'https://teerouter.org')
            try:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
                    status, value, _ = self.request(client, 'GET', base + '/merchant-api/v1/profile',
                        {'Accept': 'application/json', 'Authorization': 'Bearer ' + store.settings['token']})
                if status != 200:
                    raise RuntimeError('无法读取商户用户名，请检查管理平台连接')
                store.remember_platform_identity(base, value.get('merchant', {}).get('id'),
                    value.get('account'), legacy=value.get('legacy', False))
                if value.get("router_access"):
                    self.application.transport.remember_router_access(value["router_access"], value.get("merchant", {}).get("id"), base)
                return store.platform_identity()
            except (httpx.HTTPError, json.JSONDecodeError):
                raise RuntimeError('无法连接管理平台，请检查平台地址后重试') from None

    def bind_account(self, client, base, session, value):
        """Resolve the authenticated account locally; never return its secret to the browser."""
        app, store = self.application, self.application.store
        value['local_connected'] = False
        account = value.get('account', {})
        if (account.get('role') != 'merchant' or not isinstance(account.get('id'), str)
                or not isinstance(account.get('name'), str)
                or not isinstance(account.get('username'), str)):
            raise RuntimeError('平台账户响应无效')
        approved = {a.get('subject') for a in value.get('applications', [])
                    if a.get('kind') == 'merchant' and a.get('status') == 'approved'
                    and isinstance(a.get('subject'), str)}
        if account.get('id') != account.get('identity_id') or (approved and approved != {account['id']}):
            raise RuntimeError('平台返回的商户公钥身份不匹配')
        session['account'] = {k: account.get(k) for k in ('id', 'role', 'username', 'name', 'identity_id', 'public_key')}
        session['merchants'] = approved
        if not value.get('merchant_credentials_available'):
            session['merchants'] = set()
            return
        identity = store.enrollment()['id']
        if (not store.credentials()['saved'] or identity not in approved
                or base != platform_url(store.settings.get('platform_url'))):
            status, credentials, _ = self.request(client, 'POST', base + '/portal/api/merchant/credentials/reveal',
                {'Accept': 'application/json', 'Cookie': session['cookie'], 'X-CSRF-Token': session['csrf']})
            if status != 200:
                raise RuntimeError('商户连接未完成，请刷新重试')
            if credentials.get('merchant_id') not in approved:
                raise RuntimeError('平台返回的商户身份不匹配')
            app._api('POST', '/api/credentials', {k: credentials.get(k) for k in ('merchant_id', 'merchant_secret')})
            identity = store.enrollment()['id']
            if base != platform_url(store.settings.get('platform_url')):
                app._api('PUT', '/api/platform', {'platform_url': base})
        if value.get("router_access"):
            app.transport.remember_router_access(value["router_access"], identity, base)
        value.pop("router_access", None)
        store.remember_platform_identity(base, identity, account)
        session['identity'] = identity
        value['local_connected'] = True
        value['local_merchant_id'] = identity

    def call(self, session_id, mode, method, path, data, operation, identity):
        if mode not in ('account', 'merchant'):
            raise ValueError('账户模式无效')
        url = urlsplit(path)
        if url.scheme or url.netloc or url.fragment or len(path) > 2048:
            raise ValueError('账户接口无效')
        name = url.path
        allowed = ((method == 'GET' and name in ('info', 'me')) or
                   (method == 'POST' and name in ('login', 'register', 'logout', 'bind', 'select')))
        if mode == 'merchant':
            allowed = ((method == 'GET' and re.fullmatch(r'(me|finance|withdrawals(?:/\d+)?|receipts/(mine|records/\d+))', name)) or
                       (method == 'POST' and re.fullmatch(r'withdrawals(?:/\d+/cancel)?', name)))
        if not allowed:
            raise ValueError('不支持的账户操作')
        app = self.application
        if mode == 'merchant' and method == 'GET':
            with self.lock, app.store.lock:
                self.require(session_id, identity)
                session = self.current(session_id)
                if name == 'me':
                    value = {**session.get('view', {}), 'account': session['account'], 'keys': [],
                             'applications': session.get('view', {}).get('applications', [])}
                    return Reply(200, json.dumps(value, ensure_ascii=False).encode())
            value = app.platform_data.fetch(path)
            with self.lock, app.store.lock:
                self.require(session_id, identity)
                if self.current(session_id) is not session:
                    raise PermissionError('登录状态已变化，请重新打开页面')
            return Reply(200, json.dumps(value, ensure_ascii=False).encode())
        with app.state_lock, self.lock:
            store = app.store
            if identity != store.enrollment()['id'] and not (mode == 'account' and name == 'logout'):
                raise ValueError('商户已切换，请重新打开页面')
            base = self.base(session_id)
            key = (session_id, base)
            session = self.current(session_id) or {}
            if mode == 'account' and name in ('select', 'login'):
                return self.select(session_id, data)
            if mode == 'account' and name == 'register':
                return self.key_login(session_id, 'register')
            if mode == 'account' and name in ('me', 'bind') and session:
                if not session.get('account', {}).get('identity_id'):
                    return self.key_login(session_id, 'bind', previous=session)
                if session.get('expires', 0) <= time.time():
                    return self.key_login(session_id, 'login', session['account']['identity_id'])
            if mode == 'account' and name not in ('login', 'register', 'info', 'logout') and session.get('identity', identity) != identity and not session.get('restored'):
                return Reply(401, b'{"error":"login_required"}')
            if mode == 'account' and name in ('me', 'logout', 'password', 'bind') and not session.get('cookie'):
                return Reply(401, b'{"error":"login_required"}')
            headers = {'Accept': 'application/json'}
            if operation:
                if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', operation):
                    raise ValueError('操作编号无效')
                headers['Idempotency-Key'] = operation
            if mode == 'merchant':
                if not self.authorized(session_id, identity):
                    return Reply(401, b'{"error":"login_required"}')
                if method == 'GET' and name == 'me':
                    value = {**session.get('view', {}), 'account': session['account'], 'keys': [],
                             'applications': session.get('view', {}).get('applications', [])}
                    return Reply(200, json.dumps(value, ensure_ascii=False).encode())
                headers['Authorization'] = 'Bearer ' + store.settings['token']
                upstream = '/merchant-api/v1/' + ('profile' if name == 'me' else path)
            else:
                if name == 'register':
                    if not isinstance(data, dict) or data.get('role') != 'merchant':
                        raise ValueError('请在 teeproxy 注册用户账号')
                if session.get('cookie') and name not in ('login', 'register', 'info'):
                    headers['Cookie'] = session['cookie']
                    headers['X-CSRF-Token'] = session['csrf']
                upstream = '/portal/api/' + ('me' if name == 'bind' else path)
            # Local logout is immediate even when the platform cannot be reached.
            if mode == 'account' and name == 'logout':
                self.revoke(session_id, session)
            try:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
                    status, value, response_headers = self.request(client, 'GET' if name == 'bind' else method,
                                                                  base + upstream, headers, data)
                    if status < 300:
                        if mode == 'account' and name in ('login', 'register', 'me', 'bind'):
                            if value.get('account', {}).get('id') != session['account']['id']:
                                self.revoke(session_id, session)
                                return Reply(401, b'{"error":"login_required"}')
                            # Pending registrations bind on a later status refresh after approval.
                            try:
                                self.bind_account(client, base, session, value)
                            except (httpx.HTTPError, json.JSONDecodeError, ValueError, RuntimeError, OSError):
                                session['merchants'] = set()
                                value['local_connection_error'] = '商户连接未完成，请刷新重试'
                            session['view'] = {k: v for k, v in value.items() if k != 'csrf'}
                            renew = session['expires'] < time.time() + LIFETIME - RENEW_INTERVAL
                            restored = session.get('restored')
                            session.update(restored=False, checked=time.time())
                            if renew:
                                session['expires'] = time.time() + LIFETIME
                            if renew or restored or name in ('login', 'register', 'bind'):
                                self.persist()
                        if name == 'logout':
                            self.revoke(session_id, session)
                        value.pop('csrf', None)
                    elif status == 401 and mode == 'account':
                        if session.get('account', {}).get('identity_id') and name != 'logout':
                            return self.key_login(session_id, 'login', session['account']['identity_id'])
                        self.revoke(session_id, session)
                    return Reply(status, json.dumps(value, ensure_ascii=False).encode())
            except (httpx.HTTPError, json.JSONDecodeError):
                raise RuntimeError('无法连接管理平台，请检查平台地址后重试') from None
