'use strict';
const http=require('node:http');
const crypto=require('node:crypto');
const AccountConfig=require('../static/account-config.js');
const {PROVIDERS,officialEndpoint,authorizationUrl,exchangeRequest,normalizeTokens}=require('./oauth-providers.cjs');
const TERMINAL=new Set(['cancelled','expired','failed','saved','delivered']);
const digest=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const same=(a,b)=>{
  if(typeof a!=='string'||typeof b!=='string')return false;
  const left=Buffer.from(a),right=Buffer.from(b);
  return left.length===right.length&&crypto.timingSafeEqual(left,right);
};

async function officialJson(fetcher, provider, url, options, signal) {
  officialEndpoint(provider,url);
  let response;
  try {response=await fetcher(url,{...options,redirect:'error',credentials:'omit',cache:'no-store',signal});}
  catch {throw Error('无法连接官方授权服务，请检查本机系统代理后重新授权');}
  if(!response.ok){
    try{await response.body?.cancel();}catch{}
    // Never surface response bodies, request URLs, codes, proxy details or tokens.
    if(response.status===400||response.status===401)throw Error('官方拒绝了授权码，可能已使用或已过期；请重新授权');
    if(response.status===429)throw Error('官方授权请求过于频繁，请稍后重试');
    throw Error(`官方授权服务暂不可用（HTTP ${response.status}），请稍后重试`);
  }
  if(Number(response.headers.get('content-length'))>256*1024){try{await response.body?.cancel();}catch{}throw Error('官方授权响应过大');}
  const reader=response.body?.getReader();if(!reader)throw Error('官方授权响应为空');
  let size=0;const chunks=[];
  try{
    for(;;){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>256*1024)throw Error('官方授权响应过大');chunks.push(Buffer.from(value));}
    const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if(!value||typeof value!=='object'||Array.isArray(value))throw Error();
    return value;
  }catch(error){if(signal.aborted)throw Error('授权请求已取消或超时');if(size>256*1024)throw Error('官方授权响应过大');throw Error('官方授权响应格式无效');}
  finally{try{await reader.cancel();}catch{}}
}

class OAuthController {
  constructor({backend,fetcher,openBrowser,timeout=10*60*1000,ports={}}){
    this.backend=backend;this.fetcher=fetcher;this.openBrowser=openBrowser;this.timeout=timeout;this.ports=ports;this.active=null;
  }
  async context(identity){
    const state=await this.backend.call('GET','/api/state');
    if(!/^m_[a-f0-9]{32}$/.test(identity||'')||state.enrollment.id!==identity)throw Error('商户已切换，请重新操作');
    if(!state.credentials.saved||!state.remote||!state.settings.router)throw Error('请先绑定商户并连接一次 Router');
    return state;
  }
  async material(state,entry,editId){
    if(!editId)return null;
    const pool=state.remote.pools.find(p=>p.deployments.some(d=>d.id===editId));
    const deployment=pool?.deployments.find(d=>d.id===editId);
    if(!deployment?.oauth_account||deployment.provider==='workbuddy'||pool.name!==entry.pool)throw Error('只能为原账号池中的 OAuth 账号重新授权');
    const local=await this.backend.call('GET','/api/accounts/'+encodeURIComponent(editId),{},state.enrollment.id);
    return digest({pool:pool.name,deployment,credential:local.credential_text,proxy:local.proxy});
  }
  valid(flow){return this.active===flow&&!TERMINAL.has(flow.status)&&!flow.abort.signal.aborted;}
  require(id,identity){
    const flow=this.active;
    if(!flow||flow.identity!==identity||flow.id!==id)throw Error('授权会话不存在或已结束');
    if(Date.now()>=flow.deadline&&!TERMINAL.has(flow.status)&&flow.status!=='saving')this.end(flow,'expired','授权已超时，请重新开始');
    return flow;
  }
  status(id,identity){
    const f=this.require(id,identity);
    return {id:f.id,provider:f.provider,status:f.status,expires_at:Math.floor(f.deadline/1000),
      auth_url:f.status==='waiting'?f.url:'',manual_code:f.provider==='anthropic',error:f.error||'',notice:f.notice||'',
      ...(f.summary?{summary:f.summary}:{}),...(f.status==='saved'?{sync_state:f.syncState}:{}),account_id:f.entry.id,pool:f.entry.pool};
  }
  stopListener(flow){
    if(flow.server){flow.server.close();flow.server.closeAllConnections();flow.server=null;}
  }
  clear(flow){
    clearTimeout(flow.timer);flow.abort.abort();this.stopListener(flow);
    flow.verifier='';flow.state='';flow.nonce='';flow.url='';flow.credential=null;flow.material=null;
  }
  end(flow,status,error=''){
    this.clear(flow);flow.status=status;flow.error=error;
    if(status!=='saved')flow.summary=null;
  }
  cancel(id,identity){
    const flow=this.require(id,identity);
    if(flow.status==='saving')throw Error('正在保存账号，请等待保存结果');
    this.end(flow,'cancelled');return {cancelled:true};
  }
  dispose(){if(this.active)this.end(this.active,'cancelled');}
  beforeMutation(method,path){
    // No credential/configuration changes or identity switches while a bind is committing.
    if(!this.active||TERMINAL.has(this.active.status))return;
    if((method==='PUT'&&['/api/settings','/api/config'].includes(path))||
       (method==='POST'&&['/api/credentials','/api/merchant-profile','/api/config/sync'].includes(path))){
      if(this.active.status==='saving')throw Error('OAuth 账号正在保存，请稍后修改配置');
      this.end(this.active,'cancelled','配置已变化，请重新开始授权');
    }
  }
  async start(body,identity){
    if(this.active&&!TERMINAL.has(this.active.status))throw Error('已有授权正在进行，请先完成或取消');
    if(!body||Object.keys(body).some(k=>!['entry','edit_id','save_refresh','credentials_only'].includes(k))||typeof body.save_refresh!=='boolean'||(body.credentials_only!==undefined&&typeof body.credentials_only!=='boolean'))throw Error('授权参数无效');
    const entry=body.entry;
    if(!entry||typeof entry!=='object'||Array.isArray(entry)||Object.keys(entry).some(k=>!['id','pool','proxy','max_concurrent','rpm','enabled'].includes(k)))throw Error('账号参数无效');
    const editId=body.edit_id||null;
    if(editId!==null&&(typeof editId!=='string'||editId!==entry.id))throw Error('重新授权的账号标识无效');
    const flow={id:crypto.randomBytes(24).toString('base64url'),identity,entry:structuredClone(entry),editId,saveRefresh:body.save_refresh,credentialsOnly:body.credentials_only===true,
      status:'starting',deadline:Date.now()+this.timeout,abort:new AbortController(),credential:null};
    this.active=flow;
    flow.timer=setTimeout(()=>{if(flow.status!=='saving')this.end(flow,'expired','授权已超时，请重新开始');},this.timeout);flow.timer.unref();
    try{
      const state=await this.context(identity);if(!this.valid(flow))throw Error('授权已取消');
      const pool=state.remote.pools.find(p=>p.name===entry.pool);
      if(!pool||!Object.hasOwn(PROVIDERS,pool.provider))throw Error('请先选择 ChatGPT、Claude 或 Grok 账号池');
      flow.provider=pool.provider;flow.config={...PROVIDERS[pool.provider]};flow.router=state.settings.router;
      if(!flow.credentialsOnly)AccountConfig.build(state.remote,[{...entry,kind:'oauth',credential:{access_token:'pending-oauth'}}],{},editId);
      flow.material=await this.material(state,entry,editId);if(!this.valid(flow))throw Error('授权已取消');
      flow.verifier=crypto.randomBytes(flow.provider==='xai'?96:32).toString('base64url');
      flow.state=crypto.randomBytes(32).toString('base64url');flow.nonce=crypto.randomBytes(24).toString('base64url');
      if(flow.provider==='xai'){
        const d=await this.json(flow,flow.config.discovery,{method:'GET',headers:{Accept:'application/json'}});
        if(d.issuer!=='https://auth.x.ai')throw Error('xAI 授权服务身份不匹配');
        flow.config.authorize=officialEndpoint('xai',d.authorization_endpoint);
        flow.config.token=officialEndpoint('xai',d.token_endpoint);
      }
      if(!this.valid(flow))throw Error('授权已取消');
      flow.redirect=flow.provider==='anthropic'?flow.config.redirect:await this.listen(flow);
      if(!this.valid(flow))throw Error('授权已取消');
      const challenge=crypto.createHash('sha256').update(flow.verifier).digest('base64url');
      flow.url=authorizationUrl(flow.provider,flow.config,flow.redirect,flow.state,challenge,flow.nonce);
      flow.status='waiting';await this.open(flow.id,identity);
      return this.status(flow.id,identity);
    }catch(error){if(this.valid(flow))this.end(flow,'failed',error.message);else this.clear(flow);throw error;}
  }
  async json(flow,url,options){
    const abort=new AbortController();const cancel=()=>abort.abort();
    flow.abort.signal.addEventListener('abort',cancel,{once:true});
    if(flow.abort.signal.aborted)cancel();
    const timer=setTimeout(cancel,30000);timer.unref();
    try{return await officialJson(this.fetcher,flow.provider,url,options,abort.signal);}
    finally{clearTimeout(timer);flow.abort.signal.removeEventListener('abort',cancel);}
  }
  async open(id,identity){
    const flow=this.require(id,identity);if(flow.status!=='waiting')throw Error('授权链接已失效，请重新开始');
    try{await this.openBrowser(flow.url);flow.notice='';}catch{flow.notice='未能打开浏览器，请复制授权链接到浏览器中登录';}
    return this.status(id,identity);
  }
  async listen(flow){
    const config=flow.config,port=this.ports[flow.provider]??config.port;
    const server=http.createServer({maxHeaderSize:8192},(req,res)=>{
      const reply=(status,text)=>{
        res.writeHead(status,{'Content-Type':'text/plain; charset=utf-8','Content-Length':Buffer.byteLength(text),
          'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'",'Connection':'close'});
        res.end(text);
      };
      const expected=new URL(flow.redirect||`http://${config.host}:${server.address()?.port}${config.path}`);
      if(req.method!=='GET'||req.headers.host!==expected.host||!req.url?.startsWith('/')||req.url.startsWith('//')||req.url.length>8192)return reply(400,'Invalid callback.');
      const url=new URL(req.url,expected);
      if(url.pathname!==config.path)return reply(404,'Not found.');
      if(!this.valid(flow)||flow.status!=='waiting')return reply(410,'This authorization is no longer waiting.');
      if(url.searchParams.getAll('state').length!==1||!same(url.searchParams.get('state'),flow.state))return reply(400,'Invalid authorization state. Return to teemerchant and use the current login link.');
      if(url.searchParams.has('error')){reply(200,'Authorization was not granted. Return to teemerchant.');this.end(flow,'failed','你取消了授权，或官方没有批准这次登录');return;}
      const code=url.searchParams.get('code');
      if(url.searchParams.getAll('code').length!==1||!this.codeValid(code))return reply(400,'Missing or invalid authorization code.');
      // Reserve before responding: duplicate callbacks cannot exchange the code twice.
      flow.status='exchanging';
      reply(200,'Authorization received. Return to teemerchant to save and bind the account. You may close this tab.');
      void this.exchange(flow,code);
    });
    flow.server=server;server.headersTimeout=5000;server.requestTimeout=5000;server.setTimeout(5000);server.maxConnections=8;
    try{
      await new Promise((resolve,reject)=>{server.once('error',reject);server.listen({host:'127.0.0.1',port,exclusive:true},()=>{server.removeListener('error',reject);resolve();});});
    }catch{this.stopListener(flow);throw Error(`无法监听本机 ${port} 端口，请关闭其他正在登录的 ${config.name} 客户端后重试`);}
    server.on('error',()=>{if(this.valid(flow))this.end(flow,'failed','本机授权回调已中断，请重新授权');});
    return `http://${config.host}:${server.address().port}${config.path}`;
  }
  codeValid(code){return typeof code==='string'&&code.length>0&&code.length<=4096&&!/[\x00-\x20\x7f]/.test(code);}
  async submit(id,identity,text){
    const flow=this.require(id,identity);if(flow.status!=='waiting')throw Error('当前授权不再等待授权码');
    if(typeof text!=='string'||text.length>8192)throw Error('授权码格式无效');
    let code=text.trim(),state='';
    if(/^https?:\/\//i.test(code)){
      let url;try{url=new URL(code);}catch{throw Error('回调地址格式无效');}
      const expected=new URL(flow.redirect);
      if(url.origin!==expected.origin||url.pathname!==expected.pathname||url.username||url.password||url.searchParams.getAll('code').length!==1||url.searchParams.getAll('state').length!==1)throw Error('请粘贴本次登录的完整回调地址');
      code=url.searchParams.get('code');state=url.searchParams.get('state');
    }else if(flow.provider==='anthropic'){
      const parts=code.split('#');if(parts.length>2)throw Error('授权码格式无效');[code,state='']=parts;
    }else throw Error('请粘贴含 code 和 state 的完整回调地址');
    if((state&&!same(state,flow.state))||(!state&&flow.provider!=='anthropic'))throw Error('授权码与当前会话不匹配，请使用本次登录返回的授权码');
    if(!this.codeValid(code))throw Error('授权码格式无效');
    flow.status='exchanging';await this.exchange(flow,code);return this.status(id,identity);
  }
  async exchange(flow,code){
    try{
      const payload=await this.json(flow,flow.config.token,exchangeRequest(flow.provider,flow.config,flow.redirect,flow.verifier,flow.state,code));
      if(!this.valid(flow))return;
      const value=normalizeTokens(flow.provider,flow.config,payload,flow.saveRefresh);
      flow.credential=value.credential;flow.summary=value.summary;flow.status='ready';
      flow.verifier='';flow.state='';flow.nonce='';flow.url='';this.stopListener(flow);
    }catch(error){if(this.valid(flow))this.end(flow,'failed',error.message);}
  }
  async result(id,identity){
    const flow=this.require(id,identity);
    if(flow.status!=='ready'||!flow.credential)throw Error('请先完成官方授权');
    try{
      const state=await this.context(identity);
      if(state.settings.router!==flow.router)throw Error('Router 地址已变化，请重新授权');
      if(flow.material!==await this.material(state,flow.entry,flow.editId))throw Error('账号资料已修改，请刷新后重新授权');
      if(!this.valid(flow)||flow.status!=='ready')throw Error('授权已结束');
      if(state.remote.pools.find(p=>p.name===flow.entry.pool)?.provider!==flow.provider)throw Error('账号池已变化，请重新选择');
      if(flow.credential.expires_at<=Date.now()/1000+30)throw Error('授权令牌已过期，请重新登录');
      // Explicit one-time delivery to the originating account editor. No persistence here.
      const value={provider:flow.provider,credential:structuredClone(flow.credential)};
      this.end(flow,'delivered');return value;
    }catch(error){if(this.valid(flow))this.end(flow,'failed',error.message);throw error;}
  }
  async save(id,identity){
    const flow=this.require(id,identity);
    if(flow.credentialsOnly)throw Error('请在账号表单中保存凭证');
    if(flow.status==='saved')return this.status(id,identity);
    if(flow.status!=='ready'||!flow.credential)throw Error('请先完成官方授权');
    flow.status='saving';
    try{
      const state=await this.context(identity);
      if(state.settings.router!==flow.router)throw Error('Router 地址已变化，请重新授权绑定');
      if(flow.material!==await this.material(state,flow.entry,flow.editId))throw Error('原账号资料已修改，请刷新账号后重新绑定');
      if(!this.valid(flow))throw Error('授权已结束');
      if(flow.credential.expires_at<=Date.now()/1000+30)throw Error('授权令牌已过期，请重新登录');
      const pool=state.remote.pools.find(p=>p.name===flow.entry.pool);
      if(pool?.provider!==flow.provider)throw Error('账号池已变化，请重新选择');
      const {config}=AccountConfig.build(state.remote,[{...flow.entry,kind:'oauth',credential:flow.credential}],{},flow.editId);
      // Same local-first encrypted persistence and TEE sync path as manual import.
      const result=await this.backend.call('PUT','/api/config',config,identity);
      flow.syncState=result.local_sync?.state||'pending';this.end(flow,'saved');return this.status(id,identity);
    }catch(error){this.end(flow,'failed',error.message);throw error;}
  }
  async dispatch(action,body,identity){
    if(typeof action!=='string'||!body||typeof body!=='object'||Array.isArray(body)||Buffer.byteLength(JSON.stringify(body))>65536)throw Error('OAuth 操作无效');
    if(action==='start')return this.start(body,identity);
    if(Object.keys(body).some(k=>!['id','code'].includes(k))||typeof body.id!=='string')throw Error('授权会话标识无效');
    if(action==='status')return this.status(body.id,identity);
    if(action==='cancel')return this.cancel(body.id,identity);
    if(action==='open')return this.open(body.id,identity);
    if(action==='submit')return this.submit(body.id,identity,body.code);
    if(action==='result')return this.result(body.id,identity);
    if(action==='save')return this.save(body.id,identity);
    throw Error('不支持的 OAuth 操作');
  }
}
module.exports={OAuthController,officialJson};
