"""Only this projection may cross the merchant -> Router boundary."""
import base64
import copy
import hashlib
import json
from datetime import datetime

PROTOCOL = 'access-token-only-v1'

def claims(token):
    try:
        text = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}

def normalized(credential):
    value = copy.deepcopy(credential)
    legacy = value.get('claudeAiOauth')
    if isinstance(legacy, dict):
        value = {'access_token': legacy.get('accessToken'),
                 'refresh_token': legacy.get('refreshToken'),
                 'expires_at': legacy.get('expiresAt', 0) / 1000}
    if isinstance(value.get('expires_at'), str):
        try:
            value['expires_at'] = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise ValueError('凭证过期时间无效') from None
    return value

def access_only(credential):
    value = normalized(credential)
    token = value.get('access_token')
    if not isinstance(token, str) or not token or len(token) > 65536 or any(ord(c) <= 32 or ord(c) == 127 for c in token):
        raise ValueError('缺少有效的 access_token')
    result = {'access_token': token}
    expiry = value.get('expires_at', claims(token).get('exp'))
    if expiry is not None:
        if type(expiry) not in (int, float) or expiry <= 0:
            raise ValueError('凭证过期时间无效')
        result['expires_at'] = expiry
    account_id = value.get('account_id') or claims(value.get('id_token') or token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
    if account_id:
        if not isinstance(account_id, str) or len(account_id) > 256:
            raise ValueError('ChatGPT 工作区标识无效')
        result['account_id'] = account_id
    return result

def fingerprint(credential):
    return hashlib.sha256(access_only(credential)['access_token'].encode()).hexdigest()

def router_update(update):
    result = copy.deepcopy(update)
    # Empty pools are local drafts. Sending the complete nonempty set also
    # withdraws a previously published pool when its last account is removed.
    if 'pools' in result:
        result['pools'] = [pool for pool in result['pools'] if pool['deployments']]
        names = {pool['name'] for pool in result['pools']}
        result['pool_pricing'] = [price for price in result.get('pool_pricing', []) if price['pool'] in names]
    for account in result.get('accounts', {}).values():
        if 'credential' in account and 'api_key' not in account['credential']:
            account['credential'] = access_only(account['credential'])
    # Renewal settings from older releases now belong to the merchant only.
    for profile in result.get('compatibility', {}).values():
        for operation in ('oauth_refresh', 'oauth_discovery'):
            profile.get('requests', {}).pop(operation, None)
    return result


def split_update(update):
    """Public control never receives tokens, proxy URLs or provider headers."""
    public = router_update(update)
    material = {'accounts': public.pop('accounts', {}), 'secrets': public.pop('secrets', {}), 'proxies': {}}
    controls = {}
    for name, account in list(material['accounts'].items()):
        if 'enabled' in account:
            controls[name] = {'enabled': account.pop('enabled')}
        if not account:
            del material['accounts'][name]
    if controls:
        public['account_controls'] = controls
    # Official protocols have no merchant-defined headers or request overrides.
    if public.get('compatibility'):
        raise ValueError('公开配置不支持自定义协议或请求头')
    for pool in public.get('pools', []):
        for d in pool['deployments']:
            if d.get('proxy') and d['proxy'] != '[redacted]':
                material['proxies'][d['id']] = d['proxy']
                d['proxy'] = '[redacted]' if d['proxy'] else None
            for key in ('oauth_file', 'api_base', 'gemini_project'):
                if d.pop(key, None):
                    raise ValueError('公开配置不支持私有端点或文件路径')
    return public, material


def public_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
