"use strict";
let connectionAccount=null, connectionLoaded='', connectionEpoch=0, connectionBusy=false, connectionLoading=false;
let connectionMode=location.hash==='#connection/register'?'register':location.hash==='#connection/login'?'login':null;
let connectionError='';
const connectionScope=()=>state.enrollment.id+'@'+state.settings.platform_url;
const approvalNames={pending:'开通中',provisioning:'开通中',approved:'已开通',rejected:'未开通'};
let routerConnecting=null, routerConnectionError=null;
const routerScope=()=>JSON.stringify([accessScope(state),state?.settings.router,state?.settings.tdx_policy,state?.settings.snp_policy]);
const routerError=()=>!state?.auth?.authorized?'':state.error||(routerConnectionError?.scope===routerScope()&&!state.connected?routerConnectionError.message:'');
const routerStatus=()=>state?.connected?'已连接':routerConnecting?.scope===routerScope()?'连接中':routerError()?'连接失败':'未连接';

function updateRouterStatus(){
  const status=routerStatus(),badge=$('#router-connection-status'),header=$('header .status'),notice=$('#router-connection-error');
  if(badge){badge.textContent=status;badge.className='badge'+(state.connected?'':routerError()?' rejected':' pending');}
  if(header)header.textContent=state.auth?.restoring?'正在恢复登录':state.auth?.authorized?'Router '+status:'未登录商户';
  if(notice){notice.hidden=!routerError();notice.textContent=routerError()?'Router 连接或同步失败：'+routerError():'';}
}
function acceptRouterState(next){
  const changed=state.connected!==next.connected;
  if(accessScope(state)!==accessScope(next)){acceptState(next);render();return;}
  state=next;
  const accountsChanged=section==='accounts'&&accountsRendered!==accountViewKey();
  if((changed&&!['connection','finance','receipts'].includes(section)||accountsChanged)&&!$('#dialog').open&&!poolModelEditor&&!document.activeElement?.closest('form'))render();
  else updateRouterStatus();
}
async function connectRouterAutomatically(){
  if(!state?.auth?.authorized||state.connected||!state.settings.router||!(state.settings.tdx_policy||state.settings.snp_policy))return;
  const scope=routerScope(),epoch=dataEpoch;
  if(routerConnecting?.scope===scope)return;
  const attempt={scope};routerConnecting=attempt;routerConnectionError=null;updateRouterStatus();
  try{
    await api('/api/connect','POST',{});
    if(epoch!==dataEpoch||routerScope()!==scope)return;
    const next=await api('/api/state');
    if(epoch===dataEpoch&&routerScope()===scope)acceptRouterState(next);
  }catch(error){
    if(epoch===dataEpoch&&routerScope()===scope)routerConnectionError={scope,message:error.message};
  }finally{
    if(routerConnecting===attempt)routerConnecting=null;
    updateRouterStatus();
  }
}

function policyUpload(kind,path){
  return `<div class="policy-upload"><label id="policy-label-${kind}" for="policy-path-${kind}">${kind.toUpperCase()} 信任策略</label><input id="policy-path-${kind}" type="text" name="${kind}_policy" value="${esc(path||'')}" autocomplete="off" spellcheck="false"><input id="policy-file-${kind}" type="file" data-policy="${kind}" accept=".json,application/json" hidden><button id="policy-button-${kind}" type="button" data-action="upload-policy" data-id="${kind}" aria-labelledby="policy-label-${kind} policy-button-${kind}">浏览文件</button></div>`;
}

function resetConnectionAccount(){
  connectionAccount=null;connectionLoaded='';connectionError='';connectionMode=null;
  connectionEpoch++;
}
function savedMerchantSelector(){
  const profiles=state.auth?.accounts||[];
  if(!profiles.length)return '';
  return `<div class="saved-merchants"><label for="merchant-profile">本地 ID</label><div class="actions"><select id="merchant-profile">${profiles.map(p=>`<option value="${esc(p.id)}" ${p.active?'selected':''}>${esc(p.identity_id)}</option>`).join('')}</select>${btn('使用此 ID','merchant-switch')}</div></div>`;
}
function connectionAccountView(){
  const account=state.auth?.authenticated?(connectionAccount?.account||state.auth.account):null;
  return `<div class="panelhead"><h2>商户身份</h2>${btn('注册新 ID','identity-register','', 'primary')}</div>`+
    savedMerchantSelector()+
    (account?`<label for="merchant-identity">当前 ID<input id="merchant-identity" value="${esc(account.identity_id||'')}" readonly></label><div class="actions">${btn('退出此 ID','identity-logout')}</div>`:'')+
    (connectionError?`<p class="error" role="alert">${esc(connectionError)}</p>`:'');
}
function connection(){
  const s=state.settings;
  return heading('账号与连接','')+
    `<section id="merchant-account-panel" class="panel identity-panel">${connectionAccountView()}</section>`+
    (state.auth?.authorized?`<section class="panel"><div class="panelhead"><h2>Router 连接</h2><span id="router-connection-status" class="badge ${state.connected?'':'pending'}">${routerStatus()}</span></div><form id="connection">${field('Router HTTPS 地址','router',s.router,'url','required placeholder="https://your-router.example"')}${policyUpload('tdx',s.tdx_policy)}${policyUpload('snp',s.snp_policy)}<div class="actions"><button type="submit" class="primary">保存连接</button></div></form></section>`:'')+
    `<section class="panel"><div class="panelhead"><h2>管理平台</h2></div><form id="platform-connection">${field('管理平台地址','platform_url',s.platform_url||'','url')}<div class="actions"><button type="submit" class="primary">保存平台地址</button></div></form></section>`;
}
function renderConnectionAccount(){
  const panel=$('#merchant-account-panel');
  if(section==='connection'&&panel)panel.innerHTML=connectionAccountView();
}
async function connectionApi(path,method='GET',data){
  const finish=method==='GET'?MerchantLoading.begin():()=>{};
  try{
  const identity=state.enrollment.id;
  const response=await fetch('/account-api/'+path,{method,credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-CSRF-Token':$('meta[name=csrf]').content,
      'X-Account-Mode':'account','X-Merchant-ID':identity,'Idempotency-Key':crypto.randomUUID()},
    body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){const error=Error(result.detail||result.error||'操作失败');error.status=response.status;throw error;}
  if(state.enrollment.id!==identity&&state.enrollment.id!==result.local_merchant_id)throw Error('账号已切换，请重试');
  return result;
  }finally{finish();}
}
async function acceptConnectionAccount(result){
  const next=await api('/api/state'),changed=acceptState(next);
  connectionAccount=next.auth?.view||null;connectionLoaded=connectionScope();connectionError='';connectionMode=null;
  // Refresh only the account card unless the active merchant changed. A status
  // update must never erase connection fields the merchant is still editing.
  if(changed){section='connection';render();}
  else renderConnectionAccount();
  void connectRouterAutomatically();
}
async function loadConnectionAccount(force=false){
  if(section!=='connection'||connectionBusy||connectionLoading||(!force&&(connectionMode||connectionLoaded===connectionScope())))return;
  const epoch=connectionEpoch,scope=connectionScope();
  connectionLoading=true;
  try{
    const result=await connectionApi('me');
    if(epoch!==connectionEpoch||section!=='connection')return;
    await acceptConnectionAccount(result);
  }catch(error){
    if(epoch!==connectionEpoch||section!=='connection')return;
    connectionLoaded=scope;
    if(error.status===401){
      connectionAccount=null;
      if(state.auth?.authenticated){
        hideMerchantView();await reload();return;
      }
    }else connectionError=error.message;
    if(epoch!==connectionEpoch||section!=='connection')return;
    renderConnectionAccount();
  }finally{connectionLoading=false;}
}
async function connectionAction(action){
  if(connectionBusy&&action!=='identity-refresh')return;
  if(action==='identity-refresh')return loadConnectionAccount(true);
  if(action==='identity-logout'){
    connectionBusy=true;
    const request=connectionApi('logout','POST',{});
    hideMerchantView();
    try{await request;}finally{try{await reload();}finally{connectionBusy=false;}}
    return;
  }
  if(action!=='identity-register')return;
  connectionEpoch++;connectionError='';connectionBusy=true;
  const finish=MerchantLoading.begin();
  const buttons=[...$('#merchant-account-panel').querySelectorAll('button,select')];buttons.forEach(b=>b.disabled=true);
  try{
    const result=await connectionApi('register','POST',{});
    await acceptConnectionAccount(result);
  }catch(error){connectionError=error.message;await reload();renderConnectionAccount();}
  finally{connectionBusy=false;finish();buttons.forEach(b=>b.disabled=false);}
}
setInterval(()=>{
  const pending=connectionAccount?.applications?.some(a=>a.kind==='merchant'&&['pending','provisioning'].includes(a.status));
  if(section==='connection'&&!document.hidden&&!connectionMode&&!$('#dialog').open&&
      !document.activeElement?.closest('form')&&(pending||connectionAccount?.local_connection_error))loadConnectionAccount(true).catch(()=>{});
},5000);

// Reconcile other tabs and server-side expiry without overwriting form drafts.
let authPolling=false;
async function pollAccountAccess(){
  if(authPolling||connectionBusy||document.hidden||!state)return;
  authPolling=true;
  try{
    const next=await api('/api/state');
    acceptRouterState(next);
    await refreshPlatformRecords();
  }catch{}finally{authPolling=false;}
}
setInterval(pollAccountAccess,3000);
window.addEventListener('focus',pollAccountAccess);
document.addEventListener('visibilitychange',pollAccountAccess);
window.addEventListener('message',event=>{
  if(event.origin===location.origin&&event.source===$('.platform-frame')?.contentWindow&&event.data?.type==='merchant-login-required'){
    hideMerchantView();reload().catch(()=>{});
  }
});
