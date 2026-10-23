if(document.body.dataset.mode==='merchant'){
  document.querySelectorAll('header [data-action=logout]').forEach(n=>n.remove());
  workspace.views.finance=()=>`<div class="heading"><h1>提现</h1></div>${withdrawalsView()}`;
}else{
  const actions=document.querySelector('header .actions');
  for(const [id,label] of [['overview','入驻状态'],['security','账号安全']]){const link=document.createElement('a');link.href='/account#'+id;link.textContent=label;actions?.prepend(link);}
}
