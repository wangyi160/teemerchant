"""One credential check per changed account, through its own proxy, without inference."""
import json
import re
import time
import httpx
from credential_protocol import access_only
from oauth_network import proxy_url


MESSAGES = {
    'authentication_failed': '账号认证失败',
    'access_token_expired': '访问令牌已过期',
    'permission_denied': '官方拒绝访问（HTTP 403）',
    'rate_limited': '官方限流（HTTP 429）',
    'proxy_authentication_failed': '代理认证失败（HTTP 407）',
    'connection_failed': '无法连接账号代理或官方服务',
    'timeout': '账号认证检查超时',
    'invalid_response': '官方认证检查响应无效',
    'upstream_unavailable': '官方服务暂不可用',
    'invalid_credential': '账号凭证格式无效',
    'invalid_proxy': '账号代理配置无效或代理组件不可用',
    'sync_failed': 'Router 同步失败',
    'sync_uncertain': '上次操作未完成，结果待确认',
    'renewal_failed': '访问令牌续期失败',
    'renewal_uncertain': '续期响应未确认，Refresh Token 是否已轮换未知',
    'renewal_interrupted': '续期操作中断，结果未确认',
    'renewal_connection_failed': '续期时无法连接账号代理或官方服务',
    'renewal_connection_timeout': '续期时连接账号代理或官方服务超时',
    'renewal_proxy_unavailable': '续期时账号代理组件不可用',
    'renewal_response_timeout': '续期响应超时，响应未完整到达',
    'renewal_response_interrupted': '续期响应连接中断，响应未完整到达',
    'renewal_request_interrupted': '续期请求发送中断，结果未确认',
    'renewal_invalid_response': '续期响应格式或凭证字段无效',
    'renewal_response_too_large': '续期响应超过大小限制',
    'renewal_http_error': '续期接口返回错误',
}


class AccountError(RuntimeError):
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(MESSAGES[code] + (f'（{detail}）' if detail else ''))


def renewal_error(error):
    """Classify refresh failures without echoing exceptions or upstream bodies."""
    from oauth_network import PROVIDER_ERRORS
    status = getattr(error, 'status', None)
    provider_code = getattr(error, 'provider_code', '')
    if type(status) is int and 100 <= status <= 599:
        detail = f'HTTP {status}' + (f' · {provider_code}' if isinstance(provider_code, str) and provider_code in PROVIDER_ERRORS else '')
        return 'renewal_http_error', str(AccountError('renewal_http_error', detail))
    code = getattr(error, 'code', '')
    if isinstance(code, str) and 'renewal_' + code in MESSAGES:
        code = 'renewal_' + code
    else:
        code = ('renewal_uncertain' if getattr(error, 'uncertain', False) else
                'authentication_failed' if getattr(error, 'permanent', False) else 'renewal_failed')
    return code, str(AccountError(code))


def sync_error(error):
    """Never store exception URLs, proxy passwords, tokens or upstream bodies."""
    if isinstance(error, AccountError):
        return error.code, str(error)
    from platform_data import PlatformError
    if isinstance(error, PlatformError):
        code = error.public_code
        if re.fullmatch(r'platform_http_[1-5][0-9]{2}', code):
            return 'sync_failed', '管理平台请求失败（HTTP ' + code[-3:] + '）'
        return 'sync_failed', '管理平台暂不可用' if code == 'platform_unavailable' else '管理平台未确认公开配置'
    text = str(error)
    known = ('revision_conflict', 'account_credential_changed', 'merchant_runtime_changed',
             'merchant_locked', 'merchant_forbidden', 'access_token_only', 'invalid_account',
             'merchant_request_failed', 'merchant_request_timeout', 'router_attestation_failed',
             'invalid_session', 'invalid_request', 'native_internal_error',
             'merchant_endpoint_forbidden')
    code = next((code for code in known if code in text), '')
    status = re.search(r'Router 返回 (\d{3})', text)
    detail = ', '.join(x for x in (f'HTTP {status[1]}' if status else '', code) if x)
    if not detail:
        # A category and our own source location are useful when a local error
        # prevents transmission. Never include exception messages or locals.
        category = type(error).__name__
        if not re.fullmatch(r'[A-Za-z_]{1,64}', category):
            category = 'Exception'
        frame = error.__traceback__
        location = ''
        while frame:
            filename = frame.tb_frame.f_code.co_filename.replace('\\', '/').split('/')[-1]
            if filename in ('app.py', 'local_accounts.py', 'transport.py', 'credential_protocol.py'):
                location = f'{filename}:{frame.tb_lineno}'
            frame = frame.tb_next
        detail = '内部错误 ' + category + (f' @ {location}' if location else '')
    return 'sync_failed', str(AccountError('sync_failed', detail))


def verify(account):
    provider = account['provider']
    try:
        credential = (access_only(account['credential']) if account.get('oauth_account') else
                      {'access_token': account['credential'].get('api_key')})
        if not isinstance(credential.get('access_token'), str) or not credential['access_token']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AccountError('invalid_credential') from None
    if credential.get('expires_at', float('inf')) <= time.time():
        raise AccountError('access_token_expired')
    headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + credential['access_token']}
    if provider == 'openai':
        url, field = 'https://chatgpt.com/backend-api/codex/models?client_version=0.162.0', 'models'
        if not account.get('oauth_account'):
            url, field = 'https://api.openai.com/v1/models', 'data'
        if credential.get('account_id'):
            headers['ChatGPT-Account-Id'] = credential['account_id']
    elif provider == 'anthropic':
        url, field = 'https://api.anthropic.com/v1/models?limit=1', 'data'
        headers.update({'anthropic-version': '2023-06-01', 'anthropic-beta': 'oauth-2025-04-20',
                        'anthropic-dangerous-direct-browser-access': 'true'})
        if not account.get('oauth_account'):
            headers.pop('Authorization')
            headers.pop('anthropic-beta')
            headers['x-api-key'] = credential['access_token']
    elif provider == 'xai':
        url, field = 'https://api.x.ai/v1/language-models', 'models'
    else:
        raise AccountError('invalid_credential')
    try:
        with proxy_url(account.get('proxy', '')) as proxy:
            with httpx.Client(proxy=proxy, timeout=20, trust_env=False, follow_redirects=False) as client:
                with client.stream('GET', url, headers=headers) as response:
                    status = response.status_code
                    if status != 200:
                        code = {401: 'authentication_failed', 403: 'permission_denied',
                                407: 'proxy_authentication_failed', 429: 'rate_limited'}.get(status, 'upstream_unavailable')
                        raise AccountError(code, f'HTTP {status}' if status not in (403, 407, 429) else '')
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise AccountError('invalid_response')
                    body = json.loads(raw)
                    if not isinstance(body, dict) or body.get('error') or not isinstance(body.get(field), list):
                        raise AccountError('invalid_response')
    except AccountError:
        raise
    except httpx.TimeoutException:
        raise AccountError('timeout') from None
    except httpx.HTTPError:
        raise AccountError('connection_failed') from None
    except (json.JSONDecodeError, UnicodeError):
        raise AccountError('invalid_response') from None
    except (ValueError, RuntimeError, OSError):
        raise AccountError('invalid_proxy') from None
