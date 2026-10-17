'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const http=require('node:http');
const {OAuthController,officialJson}=require('./oauth.cjs');
const {PROVIDERS,normalizeTokens,officialEndpoint}=require('./oauth-providers.cjs');
const identity='m_'+'a'.repeat(32);
const jwt=value=>'fixture.'+Buffer.from(JSON.stringify(value)).toString('base64url')+'.signature';
const tokens=provider=>({access_token:jwt({exp:Math.floor(Date.now()/1000)+3600}),expires_in:3600,
  refresh_token:'PRIVATE_REFRESH',token_type:'Bearer',scope:'openid',
  ...(provider==='openai'?{id_token:jwt({email:'fixture@example.test','https://api.openai.com/auth':{chatgpt_account_id:'real-workspace'}})}:{})});
function fixture(provider='openai',options={}) {
  const state={enrollment:{id:identity},credentials:{saved:true},settings:{router:'https://router.example'},remote:{
    merchant:{allow_test:true},local_revision:2,settings:{revision:7,max_concurrent:16,rpm:0,tpm:0,
      customers:[],customer_defaults:{enabled:true,models:[],max_concurrent:4,rpm:0,tpm:0},pool_pricing:[{pool:'pool',multiplier_millionths:200000}]},
    pools:[{name:'pool',provider,allowed_models:['model-a'],deployments:[{id:'old',provider,oauth_account:'private-ref',enabled:false,max_concurrent:8,rpm:30,weight:3}]}],
    accounts:{data:[{name:'private-ref',enabled:false}]}}};
  const local={credential_text:JSON.stringify({provider,credential:{access_token:'OLD_ACCESS'}}),proxy:'socks5h://proxy.example:1080'};
  const writes=[],requests=[],opened=[];
  const backend={async call(method,path,body,id){
    if(method==='GET'&&path==='/api/state')return structuredClone(state);
    assert.equal(id,identity);
    if(method==='GET'&&path==='/api/accounts/old')return structuredClone(local);
    assert.equal(method,'PUT');assert.equal(path,'/api/config');writes.push(structuredClone(body));
    return {local_sync:{state:options.syncState||'synced'}};
  }};
  const fetcher=async(url,init)=>{
    requests.push({url,init});
    if(url.endsWith('openid-configuration'))return Response.json({issuer:'https://auth.x.ai',authorization_endpoint:PROVIDERS.xai.authorize,token_endpoint:PROVIDERS.xai.token});
    if(options.exchange)return options.exchange(url,init);
    return Response.json(tokens(provider));
  };
  const controller=new OAuthController({backend,fetcher,openBrowser:async url=>opened.push(url),ports:{openai:0,xai:0},...options});
  const entry={id:'new-alias',pool:'pool',proxy:'socks5h://proxy.example:1080',max_concurrent:12,rpm:9};
  const start=(overrides={})=>controller.dispatch('start',{entry,edit_id:null,save_refresh:true,...overrides},identity);
  return {controller,state,local,writes,requests,opened,entry,start};
}
function callback(url,query={},headers={}){
  const auth=new URL(url),redirect=new URL(auth.searchParams.get('redirect_uri'));
  redirect.search=new URLSearchParams({code:'TEST_CODE',state:auth.searchParams.get('state'),...query});
  return new Promise((resolve,reject)=>{
    const req=http.get({host:'127.0.0.1',port:redirect.port,path:redirect.pathname+redirect.search,headers:{Host:redirect.host,...headers}},res=>{
      let body='';res.setEncoding('utf8');res.on('data',v=>body+=v);res.on('end',()=>resolve({status:res.statusCode,body}));
    });req.on('error',reject);
  });
}
async function until(fn){for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}assert.fail('condition timed out');}
async function complete(f,s){
  if(s.provider==='anthropic')await f.controller.submit(s.id,identity,'TEST_CODE#'+new URL(s.auth_url).searchParams.get('state'));
  else assert.equal((await callback(s.auth_url)).status,200);
  await until(()=>f.controller.status(s.id,identity).status==='ready');
}
for(const provider of ['openai','anthropic','xai']){
  test(provider+' uses official PKCE flow and saves into encrypted-config path only on confirmation',async t=>{
    const f=fixture(provider);t.after(()=>f.controller.dispose());const s=await f.start();
    const auth=new URL(s.auth_url);assert.equal(auth.origin,new URL(PROVIDERS[provider].authorize).origin);
    assert.equal(auth.searchParams.get('client_id'),PROVIDERS[provider].clientId);
    assert.equal(auth.searchParams.get('code_challenge_method'),'S256');
    if(provider==='xai')assert.ok(auth.searchParams.get('nonce'));
    await complete(f,s);assert.equal(f.writes.length,0);
    const req=f.requests.find(r=>r.init.method==='POST');
    const body=provider==='anthropic'?JSON.parse(req.init.body):Object.fromEntries(new URLSearchParams(req.init.body));
    assert.equal(body.code,'TEST_CODE');assert.equal(body.redirect_uri,auth.searchParams.get('redirect_uri'));
    assert.equal(crypto.createHash('sha256').update(body.code_verifier).digest('base64url'),auth.searchParams.get('code_challenge'));
    assert.equal(req.init.redirect,'error');assert.equal(req.init.credentials,'omit');
    if(provider==='anthropic')assert.equal(body.state,auth.searchParams.get('state'));
    const status=JSON.stringify(f.controller.status(s.id,identity));
    for(const secret of ['PRIVATE_REFRESH','signature','code_verifier','TEST_CODE'])assert.ok(!status.includes(secret));
    assert.equal((await f.controller.save(s.id,identity)).status,'saved');
    const config=f.writes[0],account=config.accounts['new-alias'];
    assert.equal(account.provider,provider);assert.equal(account.proxy,f.entry.proxy);assert.equal(account.credential.refresh_token,'PRIVATE_REFRESH');
    assert.deepEqual(config.pools[0].allowed_models,['model-a']);assert.equal(config.expected_revision,7);assert.equal(config._local_revision,2);
    assert.equal(config.pool_pricing[0].multiplier_millionths,200000);
    if(provider==='openai')assert.equal(account.credential.account_id,'real-workspace');
    if(provider==='xai')assert.equal(account.credential.token_endpoint,PROVIDERS.xai.token);
    assert.equal(f.controller.active.credential,null);assert.equal(f.controller.active.verifier,'');
    await f.controller.save(s.id,identity);assert.equal(f.writes.length,1);
  });
}
test('callback rejects wrong state, Unicode state, duplicate parameters, host, path and unsupported method without consuming login',async t=>{
  const f=fixture();t.after(()=>f.controller.dispose());const s=await f.start();
  for(const state of ['wrong','中'.repeat(43),''])assert.equal((await callback(s.auth_url,{state})).status,400);
  assert.equal((await callback(s.auth_url,{}, {Host:'evil.example'})).status,400);
  const url=new URL(s.auth_url),redirect=new URL(url.searchParams.get('redirect_uri'));
  const get=path=>new Promise((resolve,reject)=>{http.get({host:'127.0.0.1',port:redirect.port,path,headers:{Host:redirect.host}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));}).on('error',reject);});
  assert.equal(await get('/unexpected'),404);
  assert.equal(await get(redirect.pathname+'?code=x&state='+url.searchParams.get('state')+'&state=duplicate'),400);
  assert.equal(f.requests.length,0);assert.equal(f.controller.status(s.id,identity).status,'waiting');await complete(f,s);
});
test('simultaneous duplicate callbacks exchange the code exactly once',async t=>{
  let release;const f=fixture('openai',{exchange:()=>new Promise(r=>release=()=>r(Response.json(tokens('openai'))))});t.after(()=>f.controller.dispose());
  const s=await f.start();assert.equal((await callback(s.auth_url)).status,200);assert.equal((await callback(s.auth_url)).status,410);
  assert.equal(f.requests.length,1);release();await until(()=>f.controller.active.status==='ready');
});
test('cancel during exchange discards late tokens and closes listener without any write',async t=>{
  let release;const f=fixture('openai',{exchange:()=>new Promise(r=>release=()=>r(Response.json(tokens('openai'))))});t.after(()=>f.controller.dispose());
  const s=await f.start();await callback(s.auth_url);f.controller.cancel(s.id,identity);release();await new Promise(r=>setTimeout(r,20));
  assert.equal(f.controller.active.status,'cancelled');assert.equal(f.controller.active.credential,null);assert.equal(f.controller.active.server,null);
  await assert.rejects(f.controller.save(s.id,identity));assert.equal(f.writes.length,0);
});
test('authorization times out and returns no credential',async t=>{
  const f=fixture('anthropic',{timeout:25});t.after(()=>f.controller.dispose());const s=await f.start();
  await until(()=>f.controller.active.status==='expired');assert.equal(f.controller.active.credential,null);
  await assert.rejects(f.controller.submit(s.id,identity,'code'));assert.equal(f.writes.length,0);
});
test('manual Claude code mismatch can retry; error body cannot expose tokens',async t=>{
  const f=fixture('anthropic',{exchange:async()=>new Response('PRIVATE_REFRESH leaked',{status:400})});t.after(()=>f.controller.dispose());const s=await f.start();
  await assert.rejects(f.controller.submit(s.id,identity,'code#wrong'),/不匹配/);
  assert.equal(f.requests.length,0);const result=await f.controller.submit(s.id,identity,'code');
  assert.equal(result.status,'failed');assert.ok(!JSON.stringify(result).includes('PRIVATE_REFRESH'));assert.equal(f.writes.length,0);
});
test('renew access-only account preserves alias, disabled state, proxy, pool policy and removes refresh token',async t=>{
  const f=fixture('xai');t.after(()=>f.controller.dispose());
  const s=await f.start({entry:{id:'old',pool:'pool'},edit_id:'old',save_refresh:false});await complete(f,s);
  await f.controller.save(s.id,identity);const c=f.writes[0];
  assert.equal(c.accounts['private-ref'].credential.refresh_token,undefined);
  assert.equal(c.accounts['private-ref'].proxy,undefined);assert.equal(c.pools[0].deployments[0].enabled,false);
  assert.equal(c.pools[0].deployments[0].max_concurrent,8);assert.equal(c.pools[0].deployments[0].weight,3);
});
test('account changed during login rejects save; unrelated fresh pool and prices are preserved',async t=>{
  const f=fixture('anthropic');t.after(()=>f.controller.dispose());
  let s=await f.start({entry:{id:'old',pool:'pool'},edit_id:'old'});await complete(f,s);f.local.credential_text='changed';
  await assert.rejects(f.controller.save(s.id,identity),/原账号资料已修改/);assert.equal(f.writes.length,0);
  s=await f.start();await complete(f,s);f.state.remote.pools.push({name:'later',provider:'xai',deployments:[]});f.state.remote.settings.pool_pricing[0].multiplier_millionths=300000;
  await f.controller.save(s.id,identity);assert.equal(f.writes[0].pools[1].name,'later');assert.equal(f.writes[0].pool_pricing[0].multiplier_millionths,300000);
});
test('identity, Router and pool provider changes cannot redirect a pending binding',async t=>{
  for(const mutate of [f=>f.state.enrollment.id='m_'+'b'.repeat(32),f=>f.state.settings.router='https://other.example',f=>f.state.remote.pools[0].provider='openai']){
    const f=fixture('anthropic');t.after(()=>f.controller.dispose());const s=await f.start();await complete(f,s);mutate(f);
    await assert.rejects(f.controller.save(s.id,identity));assert.equal(f.writes.length,0);
  }
});
test('mutating config cancels flow and saving blocks concurrent changes; pending sync reported truthfully',async t=>{
  const f=fixture('anthropic',{syncState:'pending'});t.after(()=>f.controller.dispose());let s=await f.start();
  f.controller.beforeMutation('PUT','/api/config');assert.equal(f.controller.status(s.id,identity).status,'cancelled');
  s=await f.start();await complete(f,s);const save=f.controller.save(s.id,identity);
  assert.throws(()=>f.controller.beforeMutation('POST','/api/merchant-profile'),/正在保存/);
  assert.equal((await save).sync_state,'pending');assert.equal(f.writes.length,1);
});
test('IPC validates ownership, target and inputs before network or writes',async t=>{
  const f=fixture();t.after(()=>f.controller.dispose());
  await assert.rejects(f.start({entry:{...f.entry,authorize:'https://evil.example'}}));
  await assert.rejects(f.start({entry:{...f.entry,id:'old'}}),/已存在/);
  await assert.rejects(f.start({entry:{...f.entry,proxy:''}}),/proxy/);
  const s=await f.start();
  await assert.rejects(f.start(),/已有授权/);
  for(const id of ['','wrong'])await assert.rejects(f.controller.dispatch('status',{id},identity));
  await assert.rejects(f.controller.dispatch('status',{id:s.id},'m_'+'b'.repeat(32)));
  assert.equal(f.requests.length,0);assert.equal(f.writes.length,0);
});
test('untrusted discovery endpoint never receives tokens',async t=>{
  const f=fixture('xai',{fetcher:async()=>Response.json({issuer:'https://auth.x.ai',authorization_endpoint:'https://evil.example/authorize',token_endpoint:'https://evil.example/token'})});
  t.after(()=>f.controller.dispose());await assert.rejects(f.start(),/官方地址/);assert.equal(f.opened.length,0);assert.equal(f.writes.length,0);
  for(const url of ['http://auth.x.ai/token','https://auth.x.ai.evil/token','https://user:pass@auth.x.ai/token','https://auth.x.ai:444/token'])assert.throws(()=>officialEndpoint('xai',url));
});
test('invalid expiry or missing workspace refuses credential; refresh retention is explicit',()=>{
  const config=PROVIDERS.openai,payload=tokens('openai');
  assert.equal(normalizeTokens('openai',config,payload,false).credential.refresh_token,undefined);
  assert.throws(()=>normalizeTokens('openai',config,{...payload,id_token:undefined},true),/工作区/);
  assert.throws(()=>normalizeTokens('openai',config,{...payload,expires_in:1},true),/有效期/);
  assert.throws(()=>normalizeTokens('xai',PROVIDERS.xai,{access_token:'opaque'},false),/有效期/);
  assert.equal(normalizeTokens('anthropic',PROVIDERS.anthropic,tokens('anthropic'),true).summary.automatic_refresh,false);
});
test('bounded official JSON response and network errors do not leak payload or endpoints',async()=>{
  const signal=new AbortController().signal;
  await assert.rejects(officialJson(async()=>Response.json({data:'X'.repeat(300000)}),'xai',PROVIDERS.xai.token,{},signal),/过大/);
  await assert.rejects(officialJson(async()=>{throw Error('PRIVATE_TOKEN https://proxy-secret');},'xai',PROVIDERS.xai.token,{},signal),e=>!e.message.includes('PRIVATE')&&!e.message.includes('proxy-secret'));
});
