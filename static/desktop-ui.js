// This file is included only by the Electron asset handler.
if(window.teemerchant){
  let resumeAttempted=false;
  const renderWeb=render;
  render=function(){
    renderWeb();
    if(!state)return;
    if(!resumeAttempted){
      resumeAttempted=true;
      const s=state.settings;
      if(!state.connected && state.remote && s.minimum_revision>0 && s.router && (s.tdx_policy||s.snp_policy)){
        // Restore a previously enrolled connection using its existing independent trust policy.
        // Unlock does not replay any pending account edits.
        api('/api/connect','POST',{}).then(()=>reload()).catch(error=>toast('自动连接未完成：'+error.message+'；可在连接与身份中重试'));
      }
    }
    const footer=document.querySelector('aside footer');
    footer.textContent='桌面工作台 · 本地加密保存';
    document.querySelector('header').insertAdjacentHTML('beforeend',btn('收起到托盘','desktop-minimize'));
    if(section==='connection'){
      for(const name of ['transport','tdx_policy','snp_policy']){
        const input=document.querySelector(`[name=${name}]`);
        input.insertAdjacentHTML('afterend',`<button type="button" class="secondary" data-action="desktop-file" data-id="${name}">选择文件…</button>`);
      }
      document.querySelector('[name=transport]').insertAdjacentHTML('afterend','<button type="button" class="secondary" data-action="desktop-builtin">使用内置组件</button>');
      const note=document.createElement('p');note.className='muted';note.textContent='验证组件已内置。保存连接时，所选 TEE 策略会复制到程序旁的 data/policies；配置、账号和账单也保存在 data 中。请选择你审核过的信任策略。';
      document.querySelector('#connection').append(note);
    }
  };
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-action^=desktop-]');if(!button)return;
    try{
      if(button.dataset.action==='desktop-file'){
        const selected=await window.teemerchant.chooseFile(button.dataset.id);
        if(selected)document.querySelector(`[name=${button.dataset.id}]`).value=selected;
      }
      if(button.dataset.action==='desktop-minimize')await window.teemerchant.minimize();
      if(button.dataset.action==='desktop-builtin')document.querySelector('[name=transport]').value='builtin';
    }catch(error){toast(error.message);}
  });
}
