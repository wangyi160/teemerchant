"use strict";
// OAuth only fills this local editor; the ordinary account form owns persistence.
const oauthProviderNames={openai:'ChatGPT / Codex',anthropic:'Claude / Claude Code',xai:'Grok'};
function attachOAuthForm({dialog,setup,progress,identity,available,account=null,editId='',previousRefresh=false,poolName,credential,onChange}) {
  let current=null,poll=null,active=false,busy=false,closed=false,lastView='';
  const authorize=setup.querySelector('#account-authorize');
  const error=message=>{const target=setup.querySelector('[role=alert]');if(target)target.textContent=message;};
  const provider=()=>available.find(p=>p.name===(poolName || setup.elements.pool.value))?.provider;
  const updatePool=()=>{
    setup.querySelector('#account-oauth-actions').hidden=!oauthProviderNames[provider()];
    authorize.textContent=provider()==='openai'?'设备码授权':'OAuth 授权';
    setup.elements.save_refresh.checked=account?previousRefresh:true;
    if(!active){progress.hidden=true;progress.innerHTML='';lastView='';}
  };
  updatePool();setup.elements.pool.addEventListener('change',updatePool);
  const call=async(action,body)=>{
    const epoch=dataEpoch;
    const result=await window.teemerchant.oauth(action,body,identity);
    if(epoch!==dataEpoch||closed){
      if(action==='start'&&result.ok&&result.value?.id)await window.teemerchant.oauth('cancel',{id:result.value.id},identity).catch(()=>{});
      throw Error('账号状态已变化，请重新操作');
    }
    if(result.status===401)hideMerchantView();
    if(!result.ok)throw Error(result.error);return result.value;
  };
  function lock(value){
    active=value;onChange();
  }
  function paint(){
    if(closed||!current)return;
    const signature=JSON.stringify([current.status,current.error,current.notice,current.user_code,current.auth_url]);if(signature===lastView)return;lastView=signature;
    progress.hidden=false;
    const labels={waiting:'等待授权',exchanging:'获取凭证中',delivered:'已授权',failed:'授权失败',cancelled:'已取消',expired:'授权已超时'};
    const waiting=current.status==='waiting';
    const device=current.method==='device_code';
    progress.innerHTML=`<div class="panelhead"><span role="status">${esc(labels[current.status]||'授权中')}</span>${active?'<button type="button" id="oauth-cancel">取消授权</button>':''}</div>
      ${waiting?`${device?`<label>设备码<input id="oauth-device-code" value="${esc(current.user_code)}" readonly autocomplete="off" spellcheck="false"></label>`:''}
      <div class="actions"><button type="button" id="oauth-open">打开授权页面</button><button type="button" id="oauth-copy">${device?'复制设备码':'复制授权链接'}</button></div>
      ${device?`<label>授权网址<input value="${esc(current.auth_url)}" readonly></label>`:
        `${current.manual_code||current.manual_callback?'':'<details><summary>手动回调</summary>'}<label>${current.manual_code?'授权码':'回调地址'}<input name="code" autocomplete="off" spellcheck="false" maxlength="8192"></label><button type="button" id="oauth-submit">${current.manual_code?'提交授权码':'提交回调'}</button>${current.manual_code||current.manual_callback?'':'</details>'}`}`:''}`;
    const cancel=progress.querySelector('#oauth-cancel');if(cancel)cancel.onclick=()=>operation('cancel');
    if(waiting){
      progress.querySelector('#oauth-open').onclick=()=>operation('open');
      progress.querySelector('#oauth-copy').onclick=()=>copyMerchantValue(device?current.user_code:current.auth_url).catch(e=>error(e.message));
      if(!device){
        const input=setup.elements.code;
        const submit=()=>{const code=input.value;input.value='';void operation('submit',{code});};
        progress.querySelector('#oauth-submit').onclick=submit;
        input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submit();}};
      }
    }
    if(current.error||current.notice)error(current.error||current.notice);
  }
  async function accept(value){
    current=value;
    if(closed){if(active)await call('cancel',{id:current.id}).catch(()=>{});return;}
    if(current.status==='ready'){
      const result=await call('result',{id:current.id});
      if(closed)return;
      credential.value=JSON.stringify(result,null,2);
      credential.dispatchEvent(new Event('input',{bubbles:true}));
      current={...current,status:'delivered'};lock(false);
    }else if(['cancelled','failed','expired'].includes(current.status))lock(false);
    paint();
  }
  function schedule(){
    clearTimeout(poll);if(closed||busy||!active||!current)return;
    poll=setTimeout(()=>operation('status'),700);
  }
  async function operation(action,body={}){
    if(closed||busy||!current)return;
    busy=true;clearTimeout(poll);if(action!=='status')error('');
    try{
      const value=await call(action,{id:current.id,...body});
      await accept(action==='cancel'?{...current,status:'cancelled'}:value);
    }catch(e){
      if(!closed)error(e.message);
      // A rejected manual code may be corrected without losing the current session.
      if(action==='status'||current.status==='ready'){
        await call('cancel',{id:current.id}).catch(()=>{});lock(false);
        current={...current,status:'failed'};paint();
      }
    }finally{busy=false;schedule();}
  }
  authorize.onclick=async()=>{
    if(active||closed||dialog.dataset.busy)return;
    lock(true);busy=true;error('');progress.hidden=false;progress.innerHTML='<span role="status">授权中</span>';lastView='';
    try{
      const entry={pool:poolName || setup.elements.pool.value,proxy:setup.elements.proxy.value,...(editId?{id:editId}:{})};
      await accept(await call('start',{entry,edit_id:editId||null,save_refresh:setup.elements.save_refresh.checked,credentials_only:true}));
    }catch(e){if(!closed){error(e.message);lock(false);progress.hidden=true;}}
    finally{busy=false;schedule();}
  };
  const onClose=()=>{
    closed=true;clearTimeout(poll);dialog.removeEventListener('close',onClose);
    if(active&&current)void call('cancel',{id:current.id}).catch(()=>{});
    credential.value='';
  };
  dialog.addEventListener('close',onClose);
  return {isActive:()=>active,ensureReady(){if(active)throw Error('请完成或取消授权');}};
}
const actionBeforeOAuth=action;
action=async(a,id)=>{
  if(a==='oauth-renew')return accountForm(id,'oauth');
  return actionBeforeOAuth(a,id);
};
