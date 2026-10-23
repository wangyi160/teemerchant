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
                upstream = '/portal/api/' + ('merchant/credentials/reveal' if name == 'bind' else path)
            try:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
                    with client.stream(method, base + upstream, headers=headers,
                                       **({'json': data} if method != 'GET' else {})) as response:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 16 * 1024 * 1024:
                                raise RuntimeError('账户响应过大')
                            chunks.append(chunk)
                        raw = b''.join(chunks)
                        value = json.loads(raw)
                        if response.status_code < 300:
                            if name in ('login', 'register'):
                                if value.get('account', {}).get('role') != 'merchant':
                                    self.sessions.pop(key, None)
                                    return Reply(403, json.dumps({'error': '请使用商户账号登录，用户账号请使用 teeproxy'}).encode())
                                cookie = response.headers.get('set-cookie', '').split(';')[0]
                                if not re.fullmatch(r'teerouter_portal=[A-Za-z0-9_-]{40,128}', cookie):
                                    raise RuntimeError('平台登录响应无效')
                                if len(self.sessions) >= 64:
                                    self.sessions.pop(next(iter(self.sessions)))
                                self.sessions[key] = {'cookie': cookie, 'csrf': value['csrf']}
                            if name in ('logout', 'password'):
                                self.sessions.pop(key, None)
                            if name == 'bind':
                                app._api('POST', '/api/credentials', {k: value[k] for k in ('merchant_id', 'merchant_secret')})
                                raw = b'{"saved":true}'
                            else:
                                # The platform session/CSRF values are not browser credentials.
                                if isinstance(value, dict):
                                    value.pop('csrf', None)
                                    raw = json.dumps(value, ensure_ascii=False).encode()
                        elif response.status_code == 401 and mode == 'account':
                            self.sessions.pop(key, None)
                        return Reply(response.status_code, raw)
            except (httpx.HTTPError, json.JSONDecodeError):
                raise RuntimeError('无法连接管理平台，请检查平台地址后重试') from None
