"""Bind a locally generated public identity to an approved Portal account.

Portal passwords and session cookies live only for this operation. The merchant
bearer token, upstream credentials and TEE trust policy never enter this client.
"""
from http.cookiejar import CookieJar
import ipaddress
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, Request


def portal_origin(value):
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value):
        raise ValueError('Portal 地址无效')
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if (not host or url.username or url.password or url.query or url.fragment
                or url.path not in ('', '/', '/portal', '/portal/') or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        loopback = host.lower() == 'localhost'
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if url.scheme != 'https' and not (url.scheme == 'http' and loopback):
            raise ValueError()
    except ValueError:
        raise ValueError('请填写 HTTPS Portal 地址；仅本机回环地址允许 HTTP') from None
    return url.scheme + '://' + url.netloc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class PortalClient:
    def __init__(self, origin):
        self.origin = portal_origin(origin)
        self.cookies = CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.csrf = ''

    def call(self, operation, body):
        if operation not in ('login', 'merchant/bind', 'logout'):
            raise ValueError('Portal 操作无效')
        request = Request(self.origin + '/portal/api/' + operation, method='POST',
            data=json.dumps(body).encode('utf-8'), headers={'Content-Type':'application/json',
            'Origin':self.origin, 'X-CSRF-Token':self.csrf})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except HTTPError as error:
            code = error.code
            error.close()
            messages = {401:'Portal 登录名或密码错误', 403:'Portal 拒绝该操作，请确认商户账号和登录权限',
                409:'无法绑定：请检查审核状态，或确认此本地身份是否已经登记、此账号是否已绑定其他身份',
                422:'Portal 不支持这些绑定参数，请检查平台和工作台版本', 429:'Portal 操作过于频繁，请稍后重试'}
            raise RuntimeError(messages.get(code, 'Portal 请求未确认，请检查服务地址与平台状态')) from None
        except (URLError, OSError, ValueError):
            raise RuntimeError('Portal 连接或响应异常；绑定结果尚未确认，可用同一本地身份重试') from None

    def close(self):
        self.cookies.clear()
        self.csrf = ''


def bind_workstation(store, body):
    if (not isinstance(body, dict) or set(body) != {'portal_url', 'username', 'password'}
            or not isinstance(body['username'], str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', body['username'])
            or not isinstance(body['password'], str) or not 1 <= len(body['password']) <= 512):
        raise ValueError('请填写 Portal 地址、登录名和密码')
    client = PortalClient(body['portal_url'])
    try:
        login = client.call('login', {'username':body['username'], 'password':body['password']})
        client.csrf = login.get('csrf', '')
        if not isinstance(client.csrf, str) or not 1 <= len(client.csrf) <= 256:
            raise RuntimeError('Portal 登录响应无效')
        if login.get('account', {}).get('role') != 'merchant':
            raise ValueError('请使用商户 Portal 账号登录，普通用户账号不能绑定工作台')
        application = next((a for a in login.get('applications', []) if a.get('kind') == 'merchant'), None)
        if not application or application.get('status') not in ('approved', 'provisioning'):
            raise ValueError('商户尚未通过审核，请在 Portal 查看入驻进度')
        identity = store.enrollment()
        result = client.call('merchant/bind', identity)
        if result.get('id') != identity['id'] or result.get('status') not in ('approved', 'provisioning'):
            raise RuntimeError('Portal 返回的商户绑定状态无效，请在 Portal 核对结果')
        store.settings.update(portal_url=client.origin+'/portal/', portal_username=body['username'].lower())
        store.save()
        return {k:result.get(k) for k in ('id', 'status', 'router_url')}
    finally:
        if client.csrf:
            try:
                client.call('logout', {})
            except (ValueError, RuntimeError):
                pass  # A logout failure must not misreport an already committed binding.
        client.close()
