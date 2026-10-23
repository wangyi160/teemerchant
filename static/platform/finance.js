"use strict";
let financeData=null,financeError='',financeLoading=false,financeRequest=null;
let financeQuery={start:'',end:'',pool:'',offset:0,journal_offset:0};
let financeAppliedQuery={...financeQuery};
function financeMoney(value){
  if(value==null)return '—';
  const amount=BigInt(value),absolute=amount<0n?-amount:amount;
  const fraction=(absolute%1000000n).toString().padStart(6,'0').replace(/0+$/,'').padEnd(2,'0');
  return (amount<0n?'-':'')+'$'+(absolute/1000000n).toLocaleString('en-US')+'.'+fraction;
}
async function loadFinance(redraw=true){
  if(financeRequest)return financeRequest;
  financeLoading=true;financeError='';
  financeRequest=(async()=>{
    try{financeData=await api('finance?'+new URLSearchParams(financeQuery));financeAppliedQuery={...financeQuery};if(workspace.role==='user')await loadPayments();else await loadWithdrawals();}
    catch(error){financeError=error.message;financeQuery={...financeAppliedQuery};}
    finally{financeLoading=false;financeRequest=null;if(redraw&&['overview','finance'].includes(page))render();}
  })();
  return financeRequest;
}
function financeHome(role){
  const user=role==='user',balance=state.balance;
  const amount=user?(balance?BigInt(balance.ceiling_microusd)-BigInt(balance.spent_microusd):null):financeData?.summary.charge_microusd;
  return `<section class="balance-banner"><div><span>${user?'可用余额 · USD':'累计调用收入 · USD'}</span><strong>${financeMoney(amount)}</strong><small>${user?(balance?'按实际模型调用费用扣费':'账号开通后显示余额'):'已核算应收，按请求发生时的报价计费'}</small></div><a class="button-link primary" href="/${role}#finance">${user?'充值 / 查看账单':'查看收入明细'}</a></section>${financeError?`<div class="notice warn">账务信息暂时无法更新：${esc(financeError)}</div>`:''}`;
}
function financeCards(role){
  const d=financeData,user=role==='user';
  const cards=user?[
    ['可用余额 · USD',financeMoney(d.balance?.available_microusd)],
    ['累计到账充值',financeMoney(d.credited_microusd)],
    ['累计消费',financeMoney(d.balance?.spent_microusd)],
    ['累计提现',financeMoney(d.refunded_microusd)]
  ]:[
    ['累计收入 · 应收',financeMoney(d.summary.charge_microusd)],
    ['今日收入 · 应收',financeMoney(d.summary.today_microusd)],
    ['本月收入 · 应收',financeMoney(d.summary.month_microusd)],
    ['待核算请求',String(d.summary.pending)]
  ];
  return `<div class="cards finance-cards">${cards.map(([label,value])=>`<div class="card"><small>${label}</small><strong>${value}</strong></div>`).join('')}</div>`;
}
function financePager(kind,offset,limit,hasMore){
  return `<div class="finance-pagination"><span>第 ${Math.floor(offset/limit)+1} 页</span><div class="actions"><button type="button" data-action="finance-page" data-kind="${kind}" data-id="${Math.max(0,offset-limit)}" ${!offset||financeLoading?'disabled':''}>上一页</button><button type="button" data-action="finance-page" data-kind="${kind}" data-id="${offset+limit}" ${!hasMore||financeLoading?'disabled':''}>下一页</button></div></div>`;
}
function financeBillingLabel(row){
  if(row.state!=='settled')return '待核算';
  if(['estimated','mixed'].includes(row.usage_source))return '已计费 · 含估算';
  if(row.coverage==='partial')return '已计费 · 部分用量';
  if(row.usage_source==='none')return '不计费';
  return '已核算';
}
function financeView(){
  const user=workspace.role==='user',d=financeData;
  let html=`<div class="heading"><div><h1>${user?'余额与充值':'收入'}</h1><p>${user?'查看可用余额、充值流水与模型消费。':'查看调用收入与账号池收入明细。'}</p></div></div>`;
  if(financeError)html+=`<div class="notice warn">账务信息暂时无法更新：${esc(financeError)}。请点击右上角刷新重试。</div>`;
  if(!d)return html+(financeError?'':'<p class="loading">正在读取账务记录…</p>');
  html+=financeCards(workspace.role);
  if(d.sync_error)html+='<div class="notice warn">Router 同步暂未完成，以下展示已保存的账务记录，最新调用可能尚未计入。</div>';
  if(user){
    if(!d.balance)html+='<div class="notice warn">用户账号尚未开通，开通后显示可用余额。</div>';
    html+=rechargeView()+`
      <section class="panel finance-journal"><h2>充值与提现记录</h2>${table(['时间','类型','金额 · USD','记录编号'],d.journal.data.map(r=>`<tr><td>${date(r.created_at)}</td><td>${r.kind==='credit'?'充值到账':'提现'}</td><td>${financeMoney(r.amount)}</td><td>${esc(r.id)}</td></tr>`),'暂无充值或提现记录。')}${financePager('journal_offset',d.journal.offset,d.journal.limit,d.journal.has_more)}</section>`;
  }else{
    html+=withdrawalsView();
  }
  html+=`<section class="panel finance-records"><h2>${user?'消费明细':'收入明细'}</h2><form id="finance-filter" class="finance-filters">
    <label>开始日期<input name="start" type="date" value="${esc(financeQuery.start)}"></label><label>结束日期（含当天）<input name="end" type="date" value="${esc(financeQuery.end)}"></label>
    <label>账号池<select name="pool"><option value="">全部账号池</option>${d.pools.map(pool=>`<option value="${esc(pool)}" ${financeQuery.pool===pool?'selected':''}>${esc(pool||'未选池')}</option>`).join('')}</select></label>
    <button type="submit" class="primary" ${financeLoading?'disabled':''}>筛选</button><button type="button" data-action="finance-reset" ${financeLoading?'disabled':''}>重置</button></form>
    <p class="hint">${d.filtered.requests} 笔 · 已核算 ${financeMoney(d.filtered.charge_microusd)} · 待核算 ${d.filtered.pending} 笔。日期按北京时间统计。</p>
    ${table(['时间','账号池 / 模型','状态','金额 · USD'],d.data.map(row=>`<tr><td>${date(row.created_at)}<span class="sub">记录 ${esc(row.seq)}</span></td><td>${esc(row.pool||'未选池')}<span class="sub">${esc(row.model)}</span></td><td>${badge(row.state==='settled'?'approved':'pending',financeBillingLabel(row))}<span class="sub">HTTP ${esc(row.status??'—')}${row.manual?' · 人工核算':''}</span></td><td>${financeMoney(row.charge_microusd)}</td></tr>`),'暂无符合条件的调用记录。')}
    ${financePager('offset',d.offset,d.limit,d.has_more)}</section>`;
  if(!user)html+=`<section class="panel"><h2>按账号池汇总</h2>${table(['账号池','调用笔数','待核算','应收 · USD'],d.by_pool.map(pool=>`<tr><td>${esc(pool.pool||'未选池')}</td><td>${pool.requests}</td><td>${pool.pending}</td><td>${financeMoney(pool.charge_microusd)}</td></tr>`))}<p class="hint">按当前筛选条件统计全部匹配记录。</p></section>`;
  return html+`<p class="hint">账单同步时间：${date(d.synced_at)}。金额单位为 USD，按请求发生时的实际计费记录统计。</p>`;
}
function bindFinance(currentPage){
  if(!['overview','finance'].includes(currentPage))return;
  if(workspace.role==='user'&&currentPage==='finance')bindPayments();
  if(workspace.role==='merchant'&&currentPage==='finance')bindWithdrawals();
  if(!financeData&&!financeError&&!financeLoading)loadFinance();
  const form=$('#finance-filter');
  if(form)form.onsubmit=async event=>{
    event.preventDefault();if(financeLoading)return;
    const values=Object.fromEntries(new FormData(form));
    financeQuery={...financeQuery,...values,offset:0};
    form.querySelector('[type=submit]').disabled=true;
    await loadFinance();
  };
}
async function financeAction(action,id,control){
  if(financeLoading)return;
  if(action==='finance-page'){
    const key=control.dataset.kind;if(!['offset','journal_offset'].includes(key))return;
    financeQuery={...financeQuery,[key]:Number(id)};await loadFinance();
  }
  if(action==='finance-reset'){
    financeQuery={start:'',end:'',pool:'',offset:0,journal_offset:0};await loadFinance();
  }
}
