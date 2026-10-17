"use strict";
// Desktop only. Tokens and PKCE secrets never cross this renderer bridge.
const oauthProviderNames={openai:'ChatGPT / Codex',anthropic:'Claude / Claude Code',xai:'Grok'};
async function oauthForm(editId='') {
  if(!window.teemerchant?.oauth)throw Error('请在 teemerchant 桌面程序中使用 OAuth 绑定');
  const identity=state.enrollment.id;
  const originalPool=pools().find(p=>p.deployments.some(d=>d.id===editId));
  const account=originalPool?.deployments.find(d=>d.id===editId);
  if(editId&&!account?.oauth_account)throw Error('请刷新账号列表后重试');
  const available=pools().filter(p=>oauthProviderNames[p.provider]&&(!account||p.name===originalPool.name));
  if(!available.length)throw Error('请先添加 ChatGPT、Claude 或 Grok 账号池');
  const local=account?await api('/api/accounts/'+encodeURIComponent(editId)):null;
  if(state.enrollment.id!==identity)throw Error('商户已切换，请重试');
  let previousRefresh=false;
  if(local?.credential_text){try{const c=AccountConfig.oauthInput(local.credential_text,account.provider).credential;previousRefresh=!!(c.refresh_token||c.claudeAiOauth?.refreshToken);}catch{}}
  const selected=originalPool?.name||available.find(p=>p.name===selectedAccountPool)?.name||available[0].name;
  const dialog=$('#dialog');
  dialog.classList.remove('wide');delete dialog.dataset.busy;
  dialog.innerHTML=`<h2>${account?'重新授权':'OAuth 绑定账号'}</h2><form id="oauth-setup">
    <p>在官方页面登录授权，程序自动获取凭证。账号 ID 是展示给用户的别名，无需填写真实邮箱。</p>
    <div class="grid">${poolSelect(available,selected)}${field('账号 ID（别名）','id',editId,'text',`required maxlength="128" pattern="[a-zA-Z0-9_.-]+" ${account?'readonly':''}`)}</div>
    ${field('账号网络代理','proxy',local?.proxy||'','text',`${account?'':'required'} autocomplete="off" placeholder="http(s)://、socks5h:// 或 vless://"`)}
    <p class="hint">此代理供 Router 访问上游账号。官方登录使用本机浏览器和系统网络设置。</p>
    <div class="grid">${field('并发上限','max_concurrent',account?.max_concurrent??16,'number','required min="1" max="4096"')}${field('RPM（0 为不限）','rpm',account?.rpm??0,'number','required min="0" max="10000000"')}</div>
    <label><input type="checkbox" name="save_refresh">保存 refresh token</label><p id="oauth-refresh-hint"></p>
    <p class="error" role="alert"></p><div class="actions"><button type="button" id="oauth-close">取消</button><button type="submit" class="primary">前往官方授权</button></div></form><div id="oauth-progress" hidden></div>`;
  const setup=$('#oauth-setup'),progress=$('#oauth-progress');
  attachOAuthForm({dialog,setup,progress,identity,available,account,editId,previousRefresh});
  dialog.showModal();
}
function attachOAuthForm({dialog,setup,progress,identity,available,account=null,editId='',previousRefresh=false,isOAuth=()=>true}) {
  let current=null,poll=null,busy=false,closed=false,lastView='',disabledControls=[];
  const manualSubmit=setup.onsubmit;
  const refreshHint=()=>{
    const provider=available.find(p=>p.name===setup.elements.pool.value)?.provider;
    setup.elements.save_refresh.checked=account?previousRefresh:provider!=='anthropic';
    $('#oauth-refresh-hint').textContent=provider==='anthropic'?
      'Claude 当前需在令牌到期后点击“重新授权”；即使保存 refresh token，Router 也不会自动续期。':
      '勾选后 Router 可用 refresh token 续期；不勾选则只保存访问凭证，到期后需重新授权。';
  };
  refreshHint();setup.elements.pool.addEventListener('change',refreshHint);
  const call=async(action,body)=>{
    const result=await window.teemerchant.oauth(action,body,identity);
    if(!result.ok)throw Error(result.error);return result.value;
  };
  const setBusy=value=>{
    busy=value;
    if(value){dialog.dataset.busy='true';disabledControls=Array.from(dialog.querySelectorAll('button,input,select,textarea'),el=>[el,el.disabled]);disabledControls.forEach(([el])=>el.disabled=true);}
    else{delete dialog.dataset.busy;disabledControls.forEach(([el,disabled])=>el.disabled=disabled);disabledControls=[];}
  };
  const error=message=>{const target=(progress.hidden?setup:progress).querySelector('[role=alert]');if(target)target.textContent=message;};
  const close=async()=>{
    if(busy||dialog.dataset.busy)return;
    setBusy(true);
    try{if(current&&!['saved','cancelled','failed','expired'].includes(current.status))await call('cancel',{id:current.id});dialog.close();}
    catch(e){error(e.message);}finally{if(!closed)setBusy(false);}
  };
  const onCancel=e=>{e.preventDefault();void close();};
  const onClose=()=>{
    closed=true;clearTimeout(poll);dialog.removeEventListener('cancel',onCancel);dialog.removeEventListener('close',onClose);
    if(current&&!['saved','cancelled','failed','expired'].includes(current.status))void call('cancel',{id:current.id}).catch(()=>{});
    delete dialog.dataset.busy;
  };
  dialog.addEventListener('cancel',onCancel);dialog.addEventListener('close',onClose);
  const cancelButton=setup.querySelector('#oauth-close, [data-action=close]');
  cancelButton.removeAttribute('data-action');cancelButton.id='oauth-close';cancelButton.onclick=close;
  async function operation(action,body={}) {
    if(busy)return;setBusy(true);clearTimeout(poll);
    try{
      current=await call(action,{id:current.id,...body});
      if(action==='save'){
        dialog.close();
        await reload();
        toast(current.sync_state==='synced'?'账号已加密保存并同步至 Router':'账号已加密保存，等待同步 Router');
        return;
      }
      lastView='';paint();
    }catch(e){error(e.message);}
    finally{if(!closed){setBusy(false);schedule();}}
  }
  function paint(){
    if(closed||!current)return;
    const signature=JSON.stringify([current.status,current.notice,current.error]);if(lastView===signature)return;lastView=signature;
    setup.hidden=true;progress.hidden=false;
    const waiting=current.status==='waiting',ready=current.status==='ready';
    const labels={starting:'正在准备授权',waiting:'等待官方授权',exchanging:'正在获取令牌',ready:'授权成功，等待保存',saving:'正在保存账号',failed:'授权未完成',cancelled:'授权已取消',expired:'授权已超时',saved:'账号已保存'};
    progress.innerHTML=`<div class="notice"><strong>${esc(labels[current.status]||current.status)}</strong><br>${esc(oauthProviderNames[current.provider])} · ${esc(current.pool)} / ${esc(current.account_id)}</div>
      ${waiting?`<p>${current.manual_code?'在官方页面完成登录后，将页面提供的授权码粘贴到下方。':'请在打开的官方页面登录；完成后程序会自动接收回调。'}</p><div class="actions"><button id="oauth-open">重新打开浏览器</button><button id="oauth-copy">复制授权链接</button></div>
      ${current.manual_code?'':'<details><summary>浏览器回调没有自动完成</summary>'}<form id="oauth-code-form"><label>${current.manual_code?'官方授权码':'浏览器中的完整回调地址'}<input name="code" required autocomplete="off" spellcheck="false" maxlength="8192"></label><button type="submit">提交${current.manual_code?'授权码':'回调地址'}</button></form>${current.manual_code?'':'</details>'}`:''}
      ${ready?`<p>凭证有效期至 ${esc(new Date(current.summary.expires_at*1000).toLocaleString())}。${current.summary.automatic_refresh?'已允许 Router 自动续期。':'到期后请重新授权。'}</p><p>保存后凭证写入本机加密账号库，并同步到当前 Router。${account&&!account.enabled?'此账号仍为停用状态，可在账号列表中启用。':''}</p>`:''}
      <p class="muted">${esc(current.notice)}</p><p class="error" role="alert">${esc(current.error)}</p>
      <div class="actions"><button id="oauth-finish">${ready?'取消绑定':'关闭'}</button>${ready?'<button class="primary" id="oauth-save">保存并同步</button>':''}</div>`;
    $('#oauth-finish').onclick=close;
    if(waiting){
      $('#oauth-open').onclick=()=>operation('open');
      $('#oauth-copy').onclick=()=>copyMerchantValue(current.auth_url).then(()=>toast('授权链接已复制')).catch(e=>error(e.message));
      $('#oauth-code-form').onsubmit=e=>{e.preventDefault();const input=e.target.elements.code,code=input.value;input.value='';void operation('submit',{code});};
    }
    if(ready)$('#oauth-save').onclick=()=>operation('save');
  }
  function schedule(){
    clearTimeout(poll);
    if(closed||busy||!current||['saved','cancelled','failed','expired'].includes(current.status))return;
    poll=setTimeout(async()=>{
      try{current=await call('status',{id:current.id});paint();}catch(e){error(e.message);return;}
      schedule();
    },1000);
  }
  setup.onsubmit=async e=>{
    if(!isOAuth())return manualSubmit(e);
    e.preventDefault();if(busy)return;
    const f=new FormData(setup),entry={id:f.get('id').trim(),pool:f.get('pool'),proxy:f.get('proxy').trim(),max_concurrent:Number(f.get('max_concurrent')),rpm:Number(f.get('rpm'))};
    if(setup.elements.enabled)entry.enabled=f.has('enabled');
    setBusy(true);error('');
    try{current=await call('start',{entry,edit_id:editId||null,save_refresh:f.has('save_refresh')});paint();}
    catch(e){error(e.message);}
    finally{setBusy(false);schedule();}
  };
}
const actionBeforeOAuth=action;
action=async(a,id)=>{
  if(a==='oauth-renew')return oauthForm(id);
  return actionBeforeOAuth(a,id);
};
