"""Merchant-side PKCE authorization and credential renewal; no Electron dependency."""
import base64
import copy
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from credential_protocol import claims, normalized
from oauth_network import request, official, OAuthError

PROVIDERS = {
    'openai': {'client_id':'app_EMoamEEZ73f0CkXaXp7hrann', 'authorize':'https://auth.openai.com/oauth/authorize',
        'token':'https://auth.openai.com/oauth/token','redirect':'http://localhost:1455/auth/callback',
        'scope':'openid profile email offline_access'},
    'anthropic': {'client_id':'9d1c250a-e61b-44d9-88ed-5944d1962f5e','authorize':'https://claude.ai/oauth/authorize',
        'token':'https://api.anthropic.com/v1/oauth/token','redirect':'https://platform.claude.com/oauth/code/callback',
        'scope':'org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers'},
    'xai': {'client_id':'b1a00492-073a-47ea-816f-4c329264a828','authorize':'https://auth.x.ai/oauth2/authorize',
        'token':'https://auth.x.ai/oauth2/token','redirect':'http://127.0.0.1:56121/callback',
        'discovery':'https://auth.x.ai/.well-known/openid-configuration',
        'scope':'openid profile email offline_access grok-cli:access api:access'},
}

def tokens(provider, payload, previous=None, save_refresh=True):
    if not isinstance(payload, dict) or payload.get('error'):
        raise OAuthError('官方没有返回有效凭证', uncertain=True)
    old = normalized(previous or {})
    access = payload.get('access_token')
    if not isinstance(access, str) or not access or len(access)>65536 or any(ord(c)<=32 for c in access):
        raise OAuthError('官方返回的 access token 无效', uncertain=True)
    expires = payload.get('expires_in')
    expiry = int(time.time() + expires) if type(expires) in (int,float) and 0<expires<=366*86400 else claims(access).get('exp')
    if type(expiry) not in (int,float) or not time.time()+30 < expiry <= time.time()+366*86400:
        raise OAuthError('官方返回的有效期无效', uncertain=True)
    result = {'access_token':access,'expires_at':expiry}
    refresh = payload.get('refresh_token', old.get('refresh_token'))
    if refresh is not None:
        if not isinstance(refresh,str) or not refresh or len(refresh)>65536 or any(ord(c)<=32 for c in refresh):
            raise OAuthError('官方返回的 refresh token 无效', uncertain=True)
        if save_refresh: result['refresh_token'] = refresh
    if provider == 'openai':
        account = claims(payload.get('id_token','')).get('https://api.openai.com/auth', {}).get('chatgpt_account_id') or claims(access).get('https://api.openai.com/auth', {}).get('chatgpt_account_id') or old.get('account_id')
        if not isinstance(account,str) or not account or len(account)>256:
            raise OAuthError('ChatGPT 工作区标识缺失', uncertain=True)
        if old.get('account_id') and old['account_id'] != account:
            raise OAuthError('续期响应的工作区发生变化，请重新授权', uncertain=True)
        result['account_id'] = account
    return result

def refresh(provider, credential, proxy):
    config = PROVIDERS[provider]
    old = normalized(credential)
    value = old.get('refresh_token')
    if not isinstance(value,str) or not value:
        raise OAuthError('没有 refresh token，请重新授权', permanent=True)
    body = {'grant_type':'refresh_token','refresh_token':value,'client_id':config['client_id']}
    if provider == 'openai': body['scope'] = 'openid profile email'
    result = request(provider, config['token'], proxy,
        **({'data':body} if provider=='xai' else {'json_body':body}))
    return tokens(provider, result, old)

class OAuthService:
    def __init__(self, application):
        self.application, self.flows = application, {}

    def prune(self):
        self.flows = {key:flow for key,flow in self.flows.items() if flow['deadline']>time.time()}

    def dispatch(self, action, body, identity, session):
        if not isinstance(body,dict) or identity != self.application.store.enrollment()['id']:
            raise ValueError('商户身份已变化，请刷新页面')
        now = time.time()
        self.prune()
        if action == 'start': return self.start(body, identity, session)
        flow = self.flows.get(body.get('id'))
        if not flow or flow['identity'] != identity or flow['session'] != session:
            raise ValueError('授权会话不存在或已过期')
        if action == 'cancel':
            del self.flows[flow['id']];return {'cancelled':True}
        if action == 'submit': return self.submit(flow, body.get('code'))
        if action == 'result':
            if flow['status'] != 'ready': raise ValueError('请先完成授权')
            state = self.application.remote
            if (self.application.store.settings['router'] != flow['router'] or not state
                    or not any(p['name']==flow['pool'] and p['provider']==flow['provider'] for p in state['pools'])):
                del self.flows[flow['id']];raise ValueError('账号池或 Router 已变化，请重新授权')
            value = {'provider':flow['provider'],'credential':flow['credential']}
            del self.flows[flow['id']]
            return value
        if action in ('status','open'): return self.status(flow)
        raise ValueError('OAuth 操作不存在')

    def start(self, body, identity, session):
        if len(self.flows)>=16: raise ValueError('授权会话过多，请完成或取消已有授权')
        if any(f['session']==session for f in self.flows.values()): raise ValueError('请先完成或取消已有授权')
        entry = body.get('entry', {})
        state = self.application.remote
        pool = next((p for p in (state or {}).get('pools',[]) if p['name']==entry.get('pool')),None)
        if not pool or pool['provider'] not in PROVIDERS: raise ValueError('请选择官方账号池')
        if type(body.get('save_refresh')) is not bool: raise ValueError('请选择是否保留 refresh token')
        proxy = entry.get('proxy')
        from oauth_network import proxy_url
        # Validate even a non-discovery provider before presenting authorization.
        with proxy_url(proxy): pass
        provider = pool['provider'];config = copy.deepcopy(PROVIDERS[provider])
        if provider == 'xai':
            discovered = request(provider, config['discovery'], proxy)
            if discovered.get('issuer') != 'https://auth.x.ai': raise ValueError('xAI 授权服务身份不匹配')
            config['authorize'] = official(provider,discovered.get('authorization_endpoint',''))
            config['token'] = official(provider,discovered.get('token_endpoint',''))
        verifier = secrets.token_urlsafe(96 if provider=='xai' else 32)
        state_token = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'response_type':'code','client_id':config['client_id'],'redirect_uri':config['redirect'],
            'scope':config['scope'],'code_challenge':challenge,'code_challenge_method':'S256','state':state_token}
        if provider=='openai': params.update(id_token_add_organizations='true',codex_cli_simplified_flow='true',originator='codex_cli_rs',prompt='login')
        if provider=='anthropic': params.update(code='true',prompt='login')
        if provider=='xai': params['nonce']=secrets.token_urlsafe(24)
        flow = {'id':secrets.token_urlsafe(24),'session':session,'identity':identity,'deadline':time.time()+600,
            'provider':provider,'pool':pool['name'],'proxy':proxy,'config':config,'verifier':verifier,'state':state_token,
            'save_refresh':body['save_refresh'],'status':'waiting','router':self.application.store.settings['router'],
            'url':config['authorize']+'?'+urlencode(params)}
        self.flows[flow['id']] = flow
        return self.status(flow)

    @staticmethod
    def status(flow):
        return {'id':flow['id'],'provider':flow['provider'],'status':flow['status'],'expires_at':int(flow['deadline']),
            'auth_url':flow['url'] if flow['status']=='waiting' else '', 'manual_code':flow['provider']=='anthropic',
            'manual_callback':True, 'error':flow.get('error','')}

    def submit(self, flow, text):
        if flow['status']!='waiting' or not isinstance(text,str) or len(text)>8192: raise ValueError('授权回调无效')
        code=text.strip();state=''
        if code.startswith(('http://','https://')):
            url=urlsplit(code);expected=urlsplit(flow['config']['redirect']);query=parse_qs(url.query,keep_blank_values=True)
            if (url.scheme,url.netloc,url.path)!=(expected.scheme,expected.netloc,expected.path) or any(len(query.get(k,[]))!=1 for k in ('code','state')):
                raise ValueError('请粘贴本次登录的完整回调地址')
            code,state=query['code'][0],query['state'][0]
        elif flow['provider']=='anthropic':
            parts=code.split('#')
            if len(parts)>2: raise ValueError('授权码格式无效')
            code=parts[0];state=parts[1] if len(parts)==2 else ''
        else: raise ValueError('请粘贴含 code 和 state 的完整回调地址')
        if (state and not secrets.compare_digest(state,flow['state'])) or (not state and flow['provider']!='anthropic'):
            raise ValueError('授权回调与当前会话不匹配')
        if not code or len(code)>4096 or any(ord(c)<=32 for c in code): raise ValueError('授权码格式无效')
        flow['status']='exchanging'
        body={'grant_type':'authorization_code','client_id':flow['config']['client_id'],'code':code,
            'redirect_uri':flow['config']['redirect'],'code_verifier':flow['verifier']}
        if flow['provider']=='anthropic': body['state']=flow['state']
        try:
            payload=request(flow['provider'],flow['config']['token'],flow['proxy'],
                **({'json_body':body} if flow['provider']=='anthropic' else {'data':body}))
            flow['credential']=tokens(flow['provider'],payload,save_refresh=flow['save_refresh']);flow['status']='ready'
        except (ValueError,RuntimeError) as error:
            flow['status']='failed';flow['error']=str(error)
        finally:
            flow['verifier']='';flow['state']='';flow['url']=''
        return self.status(flow)
