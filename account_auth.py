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
}


class AccountError(RuntimeError):
    def __init__(self, code, detail=''):
        self.code = code
        super().__init__(MESSAGES[code] + (f'（{detail}）' if detail else ''))


def sync_error(error):
    """Never store exception URLs, proxy passwords, tokens or upstream bodies."""
    if isinstance(error, AccountError):
        return error.code, str(error)
    text = str(error)
    known = ('revision_conflict', 'account_credential_changed', 'merchant_runtime_changed',
             'merchant_locked', 'merchant_forbidden', 'access_token_only', 'invalid_account',
             'merchant_request_failed')
    code = next((code for code in known if code in text), '')
    status = re.search(r'Router 返回 (\d{3})', text)
    detail = ', '.join(x for x in (f'HTTP {status[1]}' if status else '', code) if x)
    return 'sync_failed', str(AccountError('sync_failed', detail))


def verify(account):
    provider = account['provider']
    if provider == 'workbuddy':
        return  # Its custom endpoint is tested through Router by the existing test button.
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
