if(document.body.dataset.mode==='merchant'){
  const embeddedIncome=new URLSearchParams(location.search).get('embedded')==='income';
  if(embeddedIncome){
    document.body.dataset.embedded='income';
    const theme=document.createElement('link');theme.rel='stylesheet';theme.href='/app.css';
    document.head.insertBefore(theme,document.querySelector('link[href="/account-assets/local.css"]'));
    let previousHeight=0;
    new ResizeObserver(()=>{
      const height=Math.ceil(document.querySelector('.shell').getBoundingClientRect().height);
      if(height!==previousHeight){previousHeight=height;parent.postMessage({type:'income-withdrawals-size',height},location.origin);}
    }).observe(document.querySelector('.shell'));
  }
  document.querySelectorAll('header [data-action=logout]').forEach(n=>n.remove());
  workspace.views.finance=()=>`${embeddedIncome?'':'<div class="heading"><h1>提现</h1></div>'}${withdrawalsView()}`;
  // Query only the visible platform page; no background history download.
  loadFinance=async(redraw=true)=>{await loadWithdrawals();if(redraw&&page==='finance')render();};
  bindFinance=p=>{if(p==='finance')bindWithdrawals();};
  let historyPolling=false,lastPlatformRead=Date.now();
  async function refreshPlatformPage(force=false){
    if(historyPolling||!state||document.hidden||$('#dialog').open||withdrawalBusy||!['finance','receipts'].includes(page))return;
    if(!force&&Date.now()-lastPlatformRead<60000)return;
    historyPolling=true;lastPlatformRead=Date.now();
    try{
      if(page==='receipts'){
        const old=JSON.stringify([ownedReceipts,ownedReceiptError]);await loadOwnedReceipts(false);
        if(page==='receipts'&&!$('#dialog').open&&old!==JSON.stringify([ownedReceipts,ownedReceiptError]))render();
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
  window.refreshPlatformPage=refreshPlatformPage;
  setInterval(refreshPlatformPage,60000);
  window.addEventListener('focus',()=>refreshPlatformPage());
  document.addEventListener('visibilitychange',()=>refreshPlatformPage());
}else{
  const actions=document.querySelector('header .actions');
  for(const [id,label] of [['overview','入驻状态'],['security','账号安全']]){const link=document.createElement('a');link.href='/account#'+id;link.textContent=label;actions?.prepend(link);}
}
