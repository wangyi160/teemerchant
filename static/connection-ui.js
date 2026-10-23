"use strict";
let connectionAccount=null, connectionLoaded='', connectionEpoch=0, connectionBusy=false, connectionLoading=false;
let connectionMode=location.hash==='#connection/register'?'register':location.hash==='#connection/login'?'login':null;
let connectionError='', registrationAllowed=false;
const connectionScope=()=>state.enrollment.id+'@'+state.settings.platform_url;
const approvalNames={pending:'待审核',provisioning:'开通中',approved:'已开通',rejected:'审核未通过'};

function resetConnectionAccount(){
  connectionAccount=null;connectionLoaded='';connectionError='';connectionMode=null;
  connectionEpoch++;
}
function savedMerchantSelector(){
  const profiles=state.profiles||[];
  if(profiles.length<2)return '';
  return `<div class="saved-merchants"><label for="merchant-profile">当前服务账号</label><div class="actions"><select id="merchant-profile">${profiles.map((p,i)=>`<option value="${esc(p.merchant_id)}" ${p.active?'selected':''}>${esc(p.username||p.name||'已有商户 '+(i+1))}</option>`).join('')}</select>${btn('切换','merchant-switch')}</div></div>`;
}
function connectionAccountView(){
  const account=connectionAccount?.account,identity=state.identity||{};
  const username=account?.username||identity.username||'';
  const mode=connectionMode||(!username?'login':'overview');
  const application=connectionAccount?.applications?.find(a=>a.kind==='merchant');
  const status=application?.status||(state.credentials?.saved?'saved':'');
  let content='';
  if(mode==='login'||mode==='register'){
    const register=mode==='register';
    content=`<div class="panelhead"><h2>${register?'注册商户':'登录商户'}</h2>${username?btn('返回账号','identity-overview'):''}</div>
      <div class="identity-tabs" role="tablist" aria-label="账号操作"><button role="tab" aria-selected="${!register}" data-action="identity-login">登录</button><button role="tab" aria-selected="${register}" data-action="identity-register">注册</button></div>
      <form id="merchant-auth" class="identity-form" data-mode="${mode}">
        ${field('用户名','username','','text','required autocomplete="username" pattern="[a-zA-Z0-9_.-]{3,64}" maxlength="64"')}
        ${register?field('商户名称','name','','text','required maxlength="128" autocomplete="organization"'):''}
        ${field('密码','password','','password',`required autocomplete="${register?'new':'current'}-password" maxlength="512"${register?' minlength="12"':''}`)}
        <p class="error" role="alert">${esc(connectionError)}</p>
        <button class="primary" type="submit" ${register&&!registrationAllowed?'disabled':''}>${register?'注册':'登录'}</button>
      </form>`;
  }else if(mode==='password'){
    content=`<div class="panelhead"><h2>修改密码</h2>${btn('返回账号','identity-overview')}</div>
      <form id="merchant-password" class="identity-form">${field('当前密码','current_password','','password','required autocomplete="current-password" maxlength="512"')}
      ${field('新密码','new_password','','password','required autocomplete="new-password" minlength="12" maxlength="512"')}
      <p class="error" role="alert"></p><button class="primary" type="submit">保存新密码</button></form>`;
  }else{
    content=`<div class="panelhead"><h2>商户账号</h2><span id="merchant-approval" class="badge ${esc(status)}">${esc(approvalNames[status]||(status==='saved'?'已绑定':''))}</span></div>
      <label for="merchant-username">用户名<input id="merchant-username" value="${esc(username)}" readonly></label>
      ${application?.note?`<p>${esc(application.note)}</p>`:''}
      ${connectionAccount?.local_connection_error?`<p class="error">${esc(connectionAccount.local_connection_error)}</p>`:''}
      ${connectionError?`<p class="error" role="alert">${esc(connectionError)}</p>`:''}
      <div class="actions identity-actions">${btn(account?'切换账号':'登录账号','identity-login')}${account?btn('修改密码','identity-password')+btn('退出登录','identity-logout'):''}${application?btn('刷新状态','identity-refresh'):''}</div>`;
  }
  return content+savedMerchantSelector();
}
function connection(){
  const s=state.settings,saved=!!state.credentials?.saved;
  return heading('账号与连接','')+
    `<section id="merchant-account-panel" class="panel identity-panel">${connectionAccountView()}</section>`+
    `<section class="panel"><div class="panelhead"><h2>Router 连接</h2><span class="badge ${state.connected?'':'pending'}">${state.connected?'已连接':'未连接'}</span></div><form id="connection"><div class="grid">${field('Router HTTPS 地址','router',s.router,'url','required placeholder="https://your-router.example"')}${field('TDX 信任策略','tdx_policy',s.tdx_policy)}<label>上传 TDX 策略<input type="file" data-policy="tdx" accept=".json,application/json"></label>${field('SNP 信任策略','snp_policy',s.snp_policy)}<label>上传 SNP 策略<input type="file" data-policy="snp" accept=".json,application/json"></label></div><div class="actions"><button type="submit" class="primary">保存连接</button>${btn('验证 TEE','verify')}<button data-action="connect" ${saved?'':'disabled'}>连接商户</button></div></form></section>`+
    `<section class="panel"><div class="panelhead"><h2>管理平台</h2><span>${state.archive?.synced_at?'上次同步 '+esc(new Date(state.archive.synced_at*1000).toLocaleString()):'尚未同步'}</span></div><form id="platform-connection">${field('管理平台地址','platform_url',s.platform_url||'','url')}<div class="actions"><button type="submit" class="primary">保存平台地址</button><button type="button" data-action="sync">同步账单</button></div></form></section>`;
}
function renderConnectionAccount(){
  const panel=$('#merchant-account-panel');
  if(section==='connection'&&panel)panel.innerHTML=connectionAccountView();
}
async function connectionApi(path,method='GET',data){
  const identity=state.enrollment.id;
  const response=await fetch('/account-api/'+path,{method,credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-CSRF-Token':$('meta[name=csrf]').content,
      'X-Account-Mode':'account','X-Merchant-ID':identity,'Idempotency-Key':crypto.randomUUID()},
    body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){const error=Error(result.detail||result.error||'操作失败');error.status=response.status;throw error;}
  if(state.enrollment.id!==identity&&state.enrollment.id!==result.local_merchant_id)throw Error('账号已切换，请重试');
  return result;
}
async function acceptConnectionAccount(result){
  const current=state.enrollment.id,next=await api('/api/state');
  if(current!==next.enrollment.id){
    records=[];selectedAccountPool=null;poolModelEditor=null;
    revenueData=null;revenueQuery={start:'',end:'',pool:'',offset:'0'};
  }
  state=next;connectionAccount=result;connectionLoaded=connectionScope();connectionError='';connectionMode=null;
  // Refresh only the account card unless the active merchant changed. A status
  // update must never erase connection fields the merchant is still editing.
  if(current!==next.enrollment.id){section='connection';render();}
  else renderConnectionAccount();
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
      if(state.credentials?.saved&&!Object.keys(state.identity||{}).length){
        try{const identity=await api('/api/identity');if(epoch===connectionEpoch)state.identity=identity;}catch{}
      }
    }else connectionError=error.message;
    if(epoch!==connectionEpoch||section!=='connection')return;
    // Leave a login or registration draft intact while the initial session
    // check finishes. Identity refreshes touch no Router/platform settings.
    const form=$('#merchant-auth')||$('#merchant-password');
    const editing=form&&([...form.querySelectorAll('input')].some(input=>input.value)||form.contains(document.activeElement));
    if(!editing)renderConnectionAccount();
  }finally{connectionLoading=false;}
}
async function connectionAction(action){
  if(connectionBusy&&action!=='identity-refresh')return;
  if(action==='identity-refresh')return loadConnectionAccount(true);
  if(action==='identity-logout'){
    await connectionApi('logout','POST',{});
    connectionAccount=null;connectionMode='login';connectionEpoch++;connectionError='';renderConnectionAccount();return;
  }
  connectionEpoch++;connectionError='';
  connectionMode=action==='identity-register'?'register':action==='identity-password'?'password':action==='identity-overview'?null:'login';
  registrationAllowed=false;
  renderConnectionAccount();
  if(connectionMode==='register'){
    const epoch=connectionEpoch;
    try{
      const result=await connectionApi('info');
      if(epoch!==connectionEpoch)return;
      registrationAllowed=!!result.policy?.registration_enabled;
      const form=$('#merchant-auth');
      if(form){form.querySelector('[type=submit]').disabled=!registrationAllowed;form.querySelector('.error').textContent=registrationAllowed?'':'暂未开放注册';}
    }catch(error){const form=$('#merchant-auth');if(epoch===connectionEpoch&&form)form.querySelector('.error').textContent=error.message;}
  }
}
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['merchant-auth','merchant-password'].includes(form.id))return;
  event.preventDefault();if(connectionBusy)return;
  const mode=form.id==='merchant-password'?'password':form.dataset.mode;
  if(mode==='register'&&!registrationAllowed)return;
  const data=Object.fromEntries(new FormData(form));
  if(mode==='register')data.role='merchant';
  connectionEpoch++;connectionBusy=true;
  const controls=[...$('#merchant-account-panel').querySelectorAll('input,button,select')];
  const disabled=controls.map(control=>control.disabled);controls.forEach(control=>control.disabled=true);
  form.querySelector('.error').textContent='';
  try{
    const result=await connectionApi(mode,'POST',data);
    form.reset();
    if(mode==='password'){
      connectionAccount=null;connectionMode='login';renderConnectionAccount();toast('密码已修改，请重新登录');
    }else await acceptConnectionAccount(result);
  }catch(error){form.querySelector('.error').textContent=error.message;}
  finally{
    delete data.password;delete data.current_password;delete data.new_password;
    connectionBusy=false;controls.forEach((control,i)=>control.disabled=disabled[i]);
  }
});
setInterval(()=>{
  const pending=connectionAccount?.applications?.some(a=>a.kind==='merchant'&&['pending','provisioning'].includes(a.status));
  if(section==='connection'&&!document.hidden&&!connectionMode&&!$('#dialog').open&&
      !document.activeElement?.closest('form')&&(pending||connectionAccount?.local_connection_error))loadConnectionAccount(true).catch(()=>{});
},5000);
