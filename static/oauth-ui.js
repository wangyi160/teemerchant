"use strict";
// OAuth only fills this local editor; the ordinary account form owns persistence.
const oauthProviderNames={openai:'ChatGPT / Codex',anthropic:'Claude / Claude Code',xai:'Grok'};
function attachOAuthForm({dialog,setup,progress,identity,available,account=null,editId='',previousRefresh=false}) {
  let current=null,poll=null,active=false,busy=false,closed=false,lastView='',locked=[];
  const authorize=setup.querySelector('#account-authorize'),credential=setup.elements.credential;
  const error=message=>{const target=setup.querySelector('[role=alert]');if(target)target.textContent=message;};
  const provider=()=>available.find(p=>p.name===setup.elements.pool.value)?.provider;
  const updatePool=()=>{
    setup.querySelector('#account-oauth-actions').hidden=!oauthProviderNames[provider()];
    setup.elements.save_refresh.checked=account?previousRefresh:provider()!=='anthropic';
    if(!active){progress.hidden=true;progress.innerHTML='';lastView='';}
  };
  updatePool();setup.elements.pool.addEventListener('change',updatePool);
  const call=async(action,body)=>{
    const result=await window.teemerchant.oauth(action,body,identity);
    if(!result.ok)throw Error(result.error);return result.value;
  };
  function lock(value){
    active=value;
    if(value){
      locked=[setup.elements.pool,credential,setup.elements.save_refresh,authorize,setup.querySelector('[type=submit]')].map(el=>[el,el.disabled]);
      locked.forEach(([el])=>el.disabled=true);
    }else{locked.forEach(([el,disabled])=>el.disabled=disabled);locked=[];}
  }
  function paint(){
    if(closed||!current)return;
    const signature=JSON.stringify([current.status,current.error,current.notice]);if(signature===lastView)return;lastView=signature;
    progress.hidden=false;
    const labels={waiting:'等待授权',exchanging:'获取凭证中',delivered:'已授权',failed:'授权失败',cancelled:'已取消',expired:'授权已超时'};
    const waiting=current.status==='waiting';
    progress.innerHTML=`<div class="panelhead"><span role="status">${esc(labels[current.status]||'授权中')}</span>${active?'<button type="button" id="oauth-cancel">取消授权</button>':''}</div>
      ${waiting?`<div class="actions"><button type="button" id="oauth-open">打开授权页面</button><button type="button" id="oauth-copy">复制授权链接</button></div>
      ${current.manual_code?'':'<details><summary>手动回调</summary>'}<label>${current.manual_code?'授权码':'回调地址'}<input name="code" autocomplete="off" spellcheck="false" maxlength="8192"></label><button type="button" id="oauth-submit">${current.manual_code?'提交授权码':'提交回调'}</button>${current.manual_code?'':'</details>'}`:''}`;
    const cancel=progress.querySelector('#oauth-cancel');if(cancel)cancel.onclick=()=>operation('cancel');
    if(waiting){
      progress.querySelector('#oauth-open').onclick=()=>operation('open');
      progress.querySelector('#oauth-copy').onclick=()=>copyMerchantValue(current.auth_url).catch(e=>error(e.message));
      const input=setup.elements.code;
      const submit=()=>{const code=input.value;input.value='';void operation('submit',{code});};
      progress.querySelector('#oauth-submit').onclick=submit;
      input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();submit();}};
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
      const entry={pool:setup.elements.pool.value,...(editId?{id:editId}:{})};
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
  return {ensureReady(){if(active)throw Error('请完成或取消授权');}};
}
const actionBeforeOAuth=action;
action=async(a,id)=>{
  if(a==='oauth-renew')return accountForm(id);
  return actionBeforeOAuth(a,id);
};
