"""Local account UI bridge. Cloud sessions stay in memory; upstream secrets never leave the vault."""
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from platform_archive import platform_url


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

    def base(self, browser):
        return self.bases.get(browser) or platform_url(self.application.store.settings.get('platform_url') or 'https://teerouter.org')

    def current(self, browser):
        """A saved vault is not a login. Grants exist only in this browser session."""
        key = (browser, self.base(browser))
        session = self.sessions.get(key)
        if session and session.get('expires', 0) <= time.time():
            self.revoke(browser, session)
            return None
        return session

    def revoke(self, browser, session):
        for key, value in list(self.accounts.items()):
            if key[0] == browser and value is session:
                self.accounts.pop(key, None)
        for key, value in list(self.sessions.items()):
            if key[0] == browser and value is session:
                self.sessions.pop(key, None)

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
        with self.lock, store.lock:
            session = self.current(browser)
            allowed = self.authorized(browser)
            base = self.base(browser)
            accounts = []
            for key, value in list(self.accounts.items()):
                if value.get('expires', 0) <= time.time():
                    self.revoke(key[0], value)
                elif key[:2] == (browser, base):
                    account = value['account']
                    accounts.append({'id': account['id'], 'username': account['username'], 'active': value is session})
            auth = {'authenticated': bool(session), 'authorized': allowed, 'accounts': accounts,
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
        return {'saved': True}

    def select(self, browser, data):
        """Switch only to an account authenticated in this browser, never a cached vault."""
        key = (browser, self.base(browser), data.get('account_id'))
        session = self.accounts.get(key)
        if not session or session.get('expires', 0) <= time.time():
            if session:
                self.revoke(browser, session)
            return Reply(401, b'{"error":"login_required"}')
        identity = session.get('identity')
        if identity in session.get('merchants', ()):
            self.application._api('POST', '/api/merchant-profile', {'merchant_id': identity})
        else:
            session['identity'] = self.application.store.enrollment()['id']
        self.sessions[key[:2]] = session
        self.application.request_platform_sync()
        return Reply(200, json.dumps(session['view'], ensure_ascii=False).encode())

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
                or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', account.get('username', ''))):
            raise RuntimeError('平台账户响应无效')
        approved = {a.get('subject') for a in value.get('applications', [])
                    if a.get('kind') == 'merchant' and a.get('status') == 'approved'
                    and isinstance(a.get('subject'), str)}
        session['account'] = {k: account[k] for k in ('id', 'role', 'username', 'name')}
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
                   (method == 'POST' and name in ('login', 'register', 'logout', 'password', 'bind', 'select')))
        if mode == 'merchant':
            allowed = ((method == 'GET' and re.fullmatch(r'(me|finance|withdrawals(?:/\d+)?|receipts/(mine|records/\d+))', name)) or
                       (method == 'POST' and re.fullmatch(r'withdrawals(?:/\d+/cancel)?', name)))
        if not allowed:
            raise ValueError('不支持的账户操作')
        app = self.application
        with app.state_lock, self.lock:
            store = app.store
            if identity != store.enrollment()['id'] and not (mode == 'account' and name == 'logout'):
                raise ValueError('商户已切换，请重新打开页面')
            base = self.base(session_id)
            key = (session_id, base)
            session = self.current(session_id) or {}
            if mode == 'account' and name == 'select':
                return self.select(session_id, data)
            if mode == 'account' and name not in ('login', 'register', 'info', 'logout') and session.get('identity', identity) != identity:
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
                if method == 'GET' and (name.startswith('withdrawals') or name.startswith('receipts/')):
                    value = app.history.read(path)
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
                        if mode == 'account' and name in ('login', 'register'):
                            account = value.get('account', {})
                            if account.get('role') != 'merchant':
                                return Reply(403, json.dumps({'error': '请使用商户账号登录，用户账号请使用 teeproxy'}).encode())
                            if (not isinstance(account.get('id'), str) or not isinstance(account.get('name'), str)
                                    or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', account.get('username', ''))):
                                raise RuntimeError('平台登录响应无效')
                            cookie = response_headers.get('set-cookie', '').split(';')[0]
                            if not re.fullmatch(r'teerouter_portal=[A-Za-z0-9_-]{40,128}', cookie):
                                raise RuntimeError('平台登录响应无效')
                            if len(self.sessions) >= 64:
                                self.sessions.pop(next(iter(self.sessions)))
                            session = {'cookie': cookie, 'csrf': value['csrf'], 'identity': identity,
                                       'account': account, 'merchants': set(), 'scope': secrets.token_urlsafe(24),
                                       'expires': time.time() + 8 * 3600}
                            if len(self.accounts) >= 64:
                                oldest = next(iter(self.accounts))
                                self.revoke(oldest[0], self.accounts[oldest])
                            self.accounts[(session_id, base, account['id'])] = session
                            self.sessions[key] = session
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
                            if value.get('local_connected') and name in ('login', 'register', 'bind'):
                                app.request_platform_sync()
                        if mode == 'merchant' and method == 'POST' and name.startswith('withdrawals'):
                            try:
                                app.history.save_withdrawal(app.history.context(), value)
                            except (RuntimeError, ValueError, OSError, sqlite3.Error):
                                # The platform write already succeeded. Do not turn a local
                                # cache failure into an apparently failed financial action.
                                app.history.error = '提现已提交，等待本地记录同步'
                            app.history.last_attempt = 0
                            app.platform_wake.set()
                        if name in ('logout', 'password'):
                            self.revoke(session_id, session)
                            if name == 'password':
                                for other_key, other in list(self.accounts.items()):
                                    if other_key[1:] == (base, session['account']['id']):
                                        self.revoke(other_key[0], other)
                        value.pop('csrf', None)
                    elif status == 401 and mode == 'account':
                        self.revoke(session_id, session)
                    return Reply(status, json.dumps(value, ensure_ascii=False).encode())
            except (httpx.HTTPError, json.JSONDecodeError):
                raise RuntimeError('无法连接管理平台，请检查平台地址后重试') from None
