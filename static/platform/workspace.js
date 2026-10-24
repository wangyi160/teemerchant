"use strict";
let state=null,page=location.hash.slice(1)||'overview';
function applications(){
  const name=workspace.role==='user'?'用户开通':'商户入驻';
  return `<section class="panel"><h2>办理记录</h2>${table(['事项','状态','提交时间','审核说明'],state.applications.filter(a=>a.kind===workspace.role).map(a=>`<tr><td>${name}</td><td>${badge(a.status,a.kind==='merchant'&&a.status==='approved'?'审核通过':null)}${a.error?'<span class="sub">同步未完成，系统将自动重试</span>':''}</td><td>${date(a.created_at)}</td><td>${esc(a.note||'—')}</td></tr>`))}</section>`;
}
function security(){
  return `<div class="heading"><div><h1>账号安全</h1><p>管理登录密码和登录会话。</p></div></div><section class="panel narrow"><h2>修改登录密码</h2><form id="password-form"><label>当前密码<input name="current_password" type="password" autocomplete="current-password" required maxlength="512"></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" required minlength="12" maxlength="512"></label><div class="error" role="alert"></div><button class="primary">保存新密码</button></form><p class="hint">修改后，其他浏览器的登录会话将失效。</p></section>`;
}
function render(){
  if(!state)return;
  if(!workspace.views[page]&&page!=='security')page='overview';
  for(const link of document.querySelectorAll('[data-page]')){
    const selected=link.dataset.page===page;link.classList.toggle('selected',selected);
    if(selected){link.setAttribute('aria-current','page');$('#page-title').textContent=link.textContent;}
    else link.removeAttribute('aria-current');
  }
  $('#identity').textContent=state.account.username;
  $('#workspace-content').innerHTML=(page==='security'?security:workspace.views[page])();
  workspace.afterRender?.(page);
  if(page==='security')$('#password-form').onsubmit=async event=>{
    event.preventDefault();const form=event.target,submit=form.querySelector('button');submit.disabled=true;
    try{await api('password','POST',Object.fromEntries(new FormData(form)));form.reset();location.replace('/account/login');}
    catch(error){form.querySelector('.error').textContent=error.message;}
    finally{submit.disabled=false;}
  };
}
async function refresh(){
  const result=await api('me');
  if(result.account.role!==workspace.role){location.replace(workspacePath(result.account.role));return;}
  state=result;csrf=result.csrf;
  if(workspace.load)await workspace.load(page);
  render();
}
function modal(title,body,submit){
  const operationId=newOperationId(),dialog=$('#dialog');
  dialog.innerHTML=`<form><h2>${esc(title)}</h2>${body}<div class="error" role="alert"></div><div class="actions">${button('关闭','close')}<button class="primary" ${submit?'':'hidden'}>确认</button></div></form>`;
  dialog.showModal();dialog.querySelector('form').onsubmit=async event=>{
    event.preventDefault();if(!submit)return;const control=event.target.querySelector('.primary');control.disabled=true;
    try{await submit(new FormData(event.target),operationId);dialog.close();await refresh();}
    catch(error){dialog.querySelector('.error').textContent=error.message;}
    finally{control.disabled=false;}
  };
}
document.addEventListener('click',async event=>{
  const control=event.target.closest('[data-action]');if(!control)return;
  const {action,id}=control.dataset;control.disabled=true;
  try{
    if(action==='refresh')await refresh();
    else if(action==='logout'){
      await api('logout','POST',{});state=null;csrf='';$('#dialog').close();$('#workspace-content').replaceChildren();location.assign('/account/login');
    }else if(action==='close')$('#dialog').close();
    else if(action==='copy-router'){await copyToClipboard(state.router_url);toast('已复制 Router 地址');}
    else await workspace.action(action,id,control);
  }catch(error){toast(error.message);}
  finally{control.disabled=false;}
});
$('#dialog').addEventListener('close',()=>$('#dialog').replaceChildren());
window.addEventListener('hashchange',()=>{page=location.hash.slice(1)||'overview';render();});
window.addEventListener('pageshow',event=>{if(event.persisted)refresh().catch(error=>toast(error.message));});
refresh().catch(error=>{$('#workspace-content').innerHTML=`<div class="notice warn">${esc(error.message)}。将自动重试。</div>`;});
setInterval(()=>{
  if(!state&&document.body.dataset.mode==='merchant'){refresh().catch(()=>{});return;}
  if(state&&!document.hidden&&!$('#dialog').open&&page!=='security'&&!document.activeElement?.closest('form')&&
    (state.local_connection_error||state.applications.some(a=>a.status==='pending'||a.status==='provisioning')||state.keys.some(k=>k.state==='provisioning')))
    refresh().catch(()=>{});
},5000);
