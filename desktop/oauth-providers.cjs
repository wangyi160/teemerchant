'use strict';
// Public CLI client IDs, not client secrets. See docs/OAUTH.md for pinned references.
const PROVIDERS = Object.freeze({
  openai: Object.freeze({name:'ChatGPT',clientId:'app_EMoamEEZ73f0CkXaXp7hrann',
    authorize:'https://auth.openai.com/oauth/authorize',token:'https://auth.openai.com/oauth/token',
    scope:'openid profile email offline_access',port:1455,host:'localhost',path:'/auth/callback'}),
  anthropic: Object.freeze({name:'Claude',clientId:'9d1c250a-e61b-44d9-88ed-5944d1962f5e',
    authorize:'https://claude.ai/oauth/authorize',token:'https://api.anthropic.com/v1/oauth/token',
    redirect:'https://platform.claude.com/oauth/code/callback',
    scope:'org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers'}),
  xai: Object.freeze({name:'Grok',clientId:'b1a00492-073a-47ea-816f-4c329264a828',
    discovery:'https://auth.x.ai/.well-known/openid-configuration',
    authorize:'https://auth.x.ai/oauth2/authorize',token:'https://auth.x.ai/oauth2/token',
    scope:'openid profile email offline_access grok-cli:access api:access',port:56121,host:'127.0.0.1',path:'/callback'}),
});

function officialEndpoint(provider, value) {
  let url;
  try {url=new URL(value);} catch {throw Error('OAuth 服务地址无效');}
  const hosts={openai:['auth.openai.com'],anthropic:['claude.ai','api.anthropic.com','platform.claude.com'],xai:['auth.x.ai']};
  if(url.protocol!=='https:'||!hosts[provider]?.includes(url.hostname)||url.port||url.username||url.password||url.hash||url.search)
    throw Error('OAuth 服务地址不是预期的官方地址');
  return url.href;
}

function authorizationUrl(provider, config, redirect, state, challenge, nonce) {
  const url=new URL(officialEndpoint(provider,config.authorize));
  url.search=new URLSearchParams({response_type:'code',client_id:config.clientId,redirect_uri:redirect,
    scope:config.scope,code_challenge:challenge,code_challenge_method:'S256',state});
  if(provider==='openai'){
    for(const [key,value] of Object.entries({id_token_add_organizations:'true',codex_cli_simplified_flow:'true',originator:'codex_cli_rs',prompt:'login'}))url.searchParams.set(key,value);
  }
  if(provider==='anthropic'){url.searchParams.set('code','true');url.searchParams.set('prompt','login');}
  if(provider==='xai')url.searchParams.set('nonce',nonce);
  return url.href;
}

function exchangeRequest(provider, config, redirect, verifier, state, code) {
  const data={grant_type:'authorization_code',client_id:config.clientId,code,redirect_uri:redirect,code_verifier:verifier};
  const json=provider==='anthropic';
  if(json)data.state=state;
  return {method:'POST',headers:{Accept:'application/json','Content-Type':json?'application/json':'application/x-www-form-urlencoded'},
    body:json?JSON.stringify(data):new URLSearchParams(data).toString()};
}

function claims(token) {
  // Metadata from the TLS token response only; decoded claims never authorize IPC or callbacks.
  try {const result=JSON.parse(Buffer.from(token.split('.')[1],'base64url').toString('utf8'));return result&&typeof result==='object'&&!Array.isArray(result)?result:{};}
  catch {return {};}
}
function tokenString(value, required=false) {
  if(value==null&&!required)return undefined;
  if(typeof value!=='string'||!value.length||value.length>65536||/[\x00-\x20\x7f]/.test(value))throw Error('官方返回的令牌格式无效，请重新授权');
  return value;
}
function normalizeTokens(provider, config, payload, saveRefresh, now=Date.now()) {
  if(!payload||typeof payload!=='object'||Array.isArray(payload)||payload.error)throw Error('官方没有返回有效的 OAuth 凭证');
  const access=tokenString(payload.access_token,true), refresh=tokenString(payload.refresh_token), idToken=tokenString(payload.id_token);
  if(payload.token_type&&String(payload.token_type).toLowerCase()!=='bearer')throw Error('官方返回了不支持的令牌类型');
  const accessClaims=claims(access), idClaims=claims(idToken||'');
  const lifetime=Number(payload.expires_in), jwtExpiry=Number(accessClaims.exp);
  const expiresAt=Number.isFinite(lifetime)&&lifetime>0?Math.floor(now/1000+lifetime):jwtExpiry;
  if(!Number.isSafeInteger(expiresAt)||expiresAt<=Math.floor(now/1000)+30||expiresAt>Math.floor(now/1000)+366*86400)
    throw Error('官方返回的有效期缺失或已过期，请重新授权');
  const credential={access_token:access,expires_at:expiresAt};
  if(saveRefresh&&refresh)credential.refresh_token=refresh;
  if(idToken)credential.id_token=idToken;
  if(provider==='openai'){
    const accountId=idClaims['https://api.openai.com/auth']?.chatgpt_account_id||accessClaims['https://api.openai.com/auth']?.chatgpt_account_id;
    if(typeof accountId!=='string'||!accountId.length||accountId.length>256)throw Error('ChatGPT 授权没有返回工作区标识，请重新登录选择工作区');
    credential.account_id=accountId;
  }
  if(provider==='xai'){credential.token_endpoint=officialEndpoint(provider,config.token);credential.token_type='Bearer';}
  if(typeof payload.scope==='string'&&payload.scope.length<4096)credential.scope=payload.scope;
  const email=idClaims.email||payload.account?.email_address;
  return {credential,summary:{expires_at:expiresAt,has_refresh:!!credential.refresh_token,
    identity:typeof email==='string'&&email.length<=256?email:'',automatic_refresh:provider!=='anthropic'&&!!credential.refresh_token}};
}

module.exports={PROVIDERS,officialEndpoint,authorizationUrl,exchangeRequest,normalizeTokens};
