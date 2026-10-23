"""Local account UI bridge. Cloud sessions stay in memory; upstream secrets never leave the vault."""
import json
import re
import threading
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
        self.lock = threading.RLock()

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
        if not value.get('merchant_credentials_available'):
            return
        account = value.get('account', {})
        if (account.get('role') != 'merchant' or not isinstance(account.get('id'), str)
                or not isinstance(account.get('name'), str)
                or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', account.get('username', ''))):
            raise RuntimeError('平台账户响应无效')
        approved = {a.get('subject') for a in value.get('applications', [])
                    if a.get('kind') == 'merchant' and a.get('status') == 'approved'}
        identity = store.enrollment()['id']
        if not store.credentials()['saved'] or identity not in approved:
            status, credentials, _ = self.request(client, 'POST', base + '/portal/api/merchant/credentials/reveal',
                {'Accept': 'application/json', 'Cookie': session['cookie'], 'X-CSRF-Token': session['csrf']})
            if status != 200:
                raise RuntimeError('商户连接未完成，请刷新重试')
            if credentials.get('merchant_id') not in approved:
                raise RuntimeError('平台返回的商户身份不匹配')
            app._api('POST', '/api/credentials', {k: credentials.get(k) for k in ('merchant_id', 'merchant_secret')})
            identity = store.enrollment()['id']
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
                   (method == 'POST' and name in ('login', 'register', 'logout', 'password', 'bind')))
        if mode == 'merchant':
            allowed = ((method == 'GET' and re.fullmatch(r'(me|finance|withdrawals(?:/\d+)?|receipts/(mine|records/\d+))', name)) or
                       (method == 'POST' and re.fullmatch(r'withdrawals(?:/\d+/cancel)?', name)))
        if not allowed:
            raise ValueError('不支持的账户操作')
        app = self.application
        with app.state_lock, self.lock:
            store = app.store
            if identity != store.enrollment()['id']:
                raise ValueError('商户已切换，请重新打开页面')
            base = platform_url(store.settings.get('platform_url') or 'https://teerouter.org')
            key = (session_id, base)
            session = self.sessions.get(key, {})
            if mode == 'account' and name not in ('login', 'register', 'info') and session.get('identity', identity) != identity:
                self.sessions.pop(key, None)
                return Reply(401, b'{"error":"login_required"}')
            if mode == 'account' and name in ('me', 'logout', 'password', 'bind') and not session.get('cookie'):
                return Reply(401, b'{"error":"login_required"}')
            headers = {'Accept': 'application/json'}
            if operation:
                if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', operation):
                    raise ValueError('操作编号无效')
                headers['Idempotency-Key'] = operation
            if mode == 'merchant':
                if not store.credentials()['saved']:
                    return Reply(401, b'{"error":"merchant_credentials_required"}')
                headers['Authorization'] = 'Bearer ' + store.settings['token']
                upstream = '/merchant-api/v1/' + ('profile' if name == 'me' else path)
            else:
                if name == 'register':
                    if not isinstance(data, dict) or data.get('role') != 'merchant':
                        raise ValueError('请在 teeproxy 注册用户账号')
                if session.get('cookie'):
                    headers['Cookie'] = session['cookie']
                    headers['X-CSRF-Token'] = session['csrf']
                upstream = '/portal/api/' + ('me' if name == 'bind' else path)
            try:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
                    status, value, response_headers = self.request(client, 'GET' if name == 'bind' else method,
                                                                  base + upstream, headers, data)
                    if status < 300:
                        if mode == 'account' and name in ('login', 'register'):
                            if value.get('account', {}).get('role') != 'merchant':
                                self.sessions.pop(key, None)
                                return Reply(403, json.dumps({'error': '请使用商户账号登录，用户账号请使用 teeproxy'}).encode())
                            cookie = response_headers.get('set-cookie', '').split(';')[0]
                            if not re.fullmatch(r'teerouter_portal=[A-Za-z0-9_-]{40,128}', cookie):
                                raise RuntimeError('平台登录响应无效')
                            if len(self.sessions) >= 64:
                                self.sessions.pop(next(iter(self.sessions)))
                            session = {'cookie': cookie, 'csrf': value['csrf'], 'identity': identity}
                            self.sessions[key] = session
                        if mode == 'account' and name in ('login', 'register', 'me', 'bind'):
                            # Pending registrations bind on a later status refresh after approval.
                            try:
                                self.bind_account(client, base, session, value)
                            except (httpx.HTTPError, json.JSONDecodeError, ValueError, RuntimeError, OSError):
                                value['local_connection_error'] = '商户连接未完成，请刷新重试'
                        if name in ('logout', 'password'):
                            self.sessions.pop(key, None)
                        value.pop('csrf', None)
                    elif status == 401 and mode == 'account':
                        self.sessions.pop(key, None)
                    return Reply(status, json.dumps(value, ensure_ascii=False).encode())
            except (httpx.HTTPError, json.JSONDecodeError):
                raise RuntimeError('无法连接管理平台，请检查平台地址后重试') from None
