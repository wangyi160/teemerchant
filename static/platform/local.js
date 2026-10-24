if(document.body.dataset.mode==='merchant'){
  document.querySelectorAll('header [data-action=logout]').forEach(n=>n.remove());
  workspace.views.finance=()=>`<div class="heading"><h1>提现</h1></div>${withdrawalsView()}`;
  // Both pages read the merchant's durable local mirror. The service downloads
  // platform changes independently of whether this frame is open.
  loadFinance=async(redraw=true)=>{await loadWithdrawals();if(redraw&&page==='finance')render();};
  bindFinance=p=>{if(p==='finance')bindWithdrawals();};
  let historyPolling=false;
  async function refreshLocalHistory(){
    if(historyPolling||!state||document.hidden||$('#dialog').open||withdrawalBusy||!['finance','receipts'].includes(page))return;
    historyPolling=true;
    try{
      if(page==='receipts'){
        const old=JSON.stringify(ownedReceipts);await loadOwnedReceipts(false);
        if(page==='receipts'&&!$('#dialog').open&&old!==JSON.stringify(ownedReceipts))render();
      }else{
        const old=JSON.stringify([withdrawalData,withdrawalError]);await loadWithdrawals();
        if(page!=='finance'||$('#dialog').open||withdrawalBusy||old===JSON.stringify([withdrawalData,withdrawalError]))return;
        const form=$('#withdrawal-form'),draft=form?Object.fromEntries(new FormData(form)):null;
        const focused=document.activeElement?.closest('#withdrawal-form')?document.activeElement.name:null;
        render();
        const next=$('#withdrawal-form');
        if(draft&&next){for(const [key,value] of Object.entries(draft)){if(next.elements[key])next.elements[key].value=value;}next.elements.amount.dispatchEvent(new Event('input'));if(focused)next.elements[focused]?.focus();}
      }
    }catch{}finally{historyPolling=false;}
  }
  setInterval(refreshLocalHistory,3000);
  window.addEventListener('focus',refreshLocalHistory);
  document.addEventListener('visibilitychange',refreshLocalHistory);
}else{
  const actions=document.querySelector('header .actions');
  for(const [id,label] of [['overview','入驻状态'],['security','账号安全']]){const link=document.createElement('a');link.href='/account#'+id;link.textContent=label;actions?.prepend(link);}
}
