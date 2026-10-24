"use strict";
let revenueData = null, revenueError='', revenueLoadedAt=0,revenueRequest=null;
let revenueQuery = {start:'', end:'', pool:'', offset:'0'};
const money = value => {
  if (value === null || value === undefined) return '待核算';
  if ((typeof value === 'number' && !Number.isSafeInteger(value)) || !/^\d{1,40}$/.test(String(value))) return '金额无效';
  const n = BigInt(value), whole = (n / 1000000n).toLocaleString('en-US');
  return '$' + whole + '.' + (n % 1000000n).toString().padStart(6, '0');
};
const revenueTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '时间未知';
const revenueReasons = {
  request_incomplete:'请求未完成，用量待确认', usage_missing:'缺少上游用量',
  invalid_router_calculation:'计费记录未通过校验', missing_historic_price_or_usage:'缺少历史报价或完整用量',
  conflicting_request_records:'相同请求存在冲突记录', multiple_attempts:'多次尝试，用量待确认',
  billing_observer_unavailable:'计费观察数据不可用', usage_unavailable:'缺少可靠用量', estimation_limit_or_usage_missing:'无法可靠估算用量'
};

async function loadRevenue() {
  const epoch=dataEpoch,navigation=pageEpoch,attempt={};revenueRequest=attempt;
  const current=()=>epoch===dataEpoch&&navigation===pageEpoch&&revenueRequest===attempt;
  try{
    const result=await api('/api/revenue?'+new URLSearchParams(revenueQuery));
    if(!current())return;
    revenueData=result;revenueError='';revenueLoadedAt=Date.now();return true;
  }catch(error){if(current()){revenueError=error.message;throw error;}}
  finally{if(revenueRequest===attempt)revenueRequest=null;}
}

function revenueSummary() {
  if(!revenueData)return `<p>${esc(revenueError||'正在读取收入…')}</p>`;
  const s=revenueData.summary;
  const card=(label,value,note)=>`<div class="card"><small>${label}</small><strong>${value}</strong><small>${note}</small></div>`;
  return (revenueError?`<p class="error">${esc(revenueError)} · 以下为上次查询结果</p>`:'')+
    `<div class="cards revenue-cards">
    ${card('累计应收 · USD',money(s.charge_microusd),`${s.calculated} 笔金额已确认`)}
    ${card('今日应收 · USD',money(s.today_microusd),'按北京时间')}
    ${card('本月应收 · USD',money(s.month_microusd),'按北京时间')}
    ${card('待核算请求',s.pending,'未计入应收合计')}
    </div>`;
}

function revenueView() {
  return heading('收入','')+`<div id="income-page" data-epoch="${pageEpoch}">
    <div id="income-summary">${revenueSummary()}</div>
    <iframe class="platform-frame income-withdrawals" title="收入提现" src="/account/finance?embedded=income#finance"></iframe>
    <div id="income-ledger">${revenueLedger()}</div></div>`;
}

function renderIncome(){
  $('#income-summary').innerHTML=revenueSummary();
  $('#income-ledger').innerHTML=revenueLedger();
  bindRevenueFilters();
}

function revenueLedger() {
  if(!revenueData)return '';
  const r=revenueData;
  const rows=r.data.map((row,i)=>`<tr>
    <td>${esc(revenueTime(row.created_at))}<small>${btn('查看账单','revenue-detail',String(i))}</small></td>
    <td>${esc(row.pool||'未选池')}<small>${esc(row.model)}</small></td>
    <td class="revenue-user">${esc(row.user_id||'未知')}</td>
    <td>${row.multiplier_millionths===null?'—':usd(row.multiplier_millionths)+'×'}</td>
    <td class="money">${money(row.charge_microusd)}</td>
    <td><span class="${row.state==='pending'?'error':'badge'}">${row.state==='pending'?'待核算':row.basis==='not_forwarded'?'未转发 · 不计费':['estimated','mixed'].includes(row.usage_source)?'已计费 · 含估算':row.coverage==='partial'?'已计费 · 部分用量':row.usage_source==='none'?'不计费':'已计费'}</span><small>HTTP ${row.status??'—'}${row.state==='pending'?'<br>'+esc(revenueReasons[row.reason]||row.reason):''}</small></td>
  </tr>`);
  return `<section class="panel"><div class="panelhead"><h2>收入明细</h2><span>${r.summary.requests} 笔</span></div><form id="revenue-filter" class="revenue-filter">
      ${field('开始日期','start',revenueQuery.start,'date')}${field('结束日期（含当天）','end',revenueQuery.end,'date')}
      <label>账号池<select name="pool"><option value="">全部账号池</option>${r.pools.map(p=>`<option value="${esc(p)}" ${p===revenueQuery.pool?'selected':''}>${esc(p||'未选池')}</option>`).join('')}</select></label>
      <button type="submit" class="primary">筛选</button>${btn('重置','revenue-reset')}
    </form><div class="panelhead"><h2>筛选结果</h2><span>${r.filtered.requests} 笔 · 应收 <strong class="money">${money(r.filtered.charge_microusd)}</strong> · 待核算 ${r.filtered.pending} 笔</span></div>
      ${table(['账号池','请求数','已计费','待核算','应收 · USD'],r.by_pool.map(p=>`<tr><td>${esc(p.pool||'未选池')}</td><td>${p.requests}</td><td>${p.calculated}</td><td>${p.pending}</td><td class="money">${money(p.charge_microusd)}</td></tr>`))}</section>
    <section class="panel"><div class="panelhead"><h2>逐笔账单</h2><small>倍率和金额均为请求发生时的记录</small></div>
      ${table(['时间 / 明细','账号池 / 模型','用户','实际倍率','应收 · USD','计费状态'],rows)}
      <div class="revenue-pagination"><span>${r.filtered.requests?`${r.offset+1}–${r.offset+r.data.length} / ${r.filtered.requests}`:'暂无账单'}</span><div class="actions">
        <button data-action="revenue-page" data-id="${Math.max(0,r.offset-r.limit)}" ${r.offset?'':'disabled'}>上一页</button>
        <button data-action="revenue-page" data-id="${r.offset+r.limit}" ${r.has_more?'':'disabled'}>下一页</button></div></div></section>
    <p>查询时间 ${esc(revenueTime(revenueLoadedAt/1000))}</p>`;
}

window.addEventListener('message',event=>{
  const frame=$('.income-withdrawals');
  if(event.origin!==location.origin||event.source!==frame?.contentWindow||event.data?.type!=='income-withdrawals-size')return;
  const height=event.data.height;
  if(Number.isFinite(height)&&height>0)frame.style.height=Math.min(30000,Math.max(120,height))+'px';
});

function bindRevenueFilters() {
  const form=$('#revenue-filter');if(!form)return;
  form.querySelector('[data-action=revenue-reset]').type='button';
  form.onsubmit=async e=>{
    e.preventDefault();const data=new FormData(form),previous={...revenueQuery};
    revenueQuery={start:data.get('start'),end:data.get('end'),pool:data.get('pool'),offset:'0'};
    const button=form.querySelector('[type=submit]');button.disabled=true;
    try{if(await loadRevenue())render();}catch(error){revenueQuery=previous;toast(error.message);}
    finally{button.disabled=false;}
  };
}

async function revenueDetail(index) {
  const row=revenueData.data[Number(index)];if(!row)return;
  const record=await api('/api/revenue/record?'+new URLSearchParams({instance:row.instance,seq:row.seq}));
  const event=record.event||{},bill=event.billing||{},quote=event.merchant_pricing||{};
  const attempts=Array.isArray(bill.attempt_records)?bill.attempt_records:[];
  const items=attempts.length?attempts.flatMap(a=>(a.billing?.items||[]).map(i=>({...i,attempt:a.number}))):(Array.isArray(bill.items)?bill.items.filter(item=>item&&typeof item==='object'):[]);
  const kinds={input:'输入',output:'输出',cache_read:'缓存读取',cache_write:'缓存写入',cache_write_hour:'缓存写入（1 小时）'};
  const dialog=$('#dialog');dialog.classList.add('wide');
  dialog.innerHTML=`<h2>调用账单</h2><div class="panelhead"><span>${esc(row.pool)} / ${esc(row.model)}</span><strong class="money">应收 ${money(row.charge_microusd)}</strong></div>
    <p>${esc(revenueTime(row.created_at))} · HTTP ${row.status??'—'} · ${row.multiplier_millionths===null?'倍率未知':usd(row.multiplier_millionths)+'×'}<br>
    ${row.state==='pending'?esc(revenueReasons[row.reason]||row.reason):row.basis==='platform_settlement'?'管理平台已结算':row.basis==='not_forwarded'?'请求未转发，不计费':'使用请求时的价格快照'}<br>
    用户：${esc(row.user_id)}<br>请求：<code>${esc(row.request_id)}</code><br>
    商户价格版本 ${esc(row.price_version??'—')} · 官方价格版本 ${esc(row.official_price_revision??'—')}</p>
    ${table(['尝试','计费项目','用量','官方单价 · USD','单位'],items.map(item=>`<tr><td>${esc(item.attempt||1)}</td><td>${esc(kinds[item.kind]||item.kind)}</td><td>${esc(item.quantity)}</td><td class="money">${money(item.unit_price_microusd)}</td><td>${item.unit==='million_tokens'?'每百万 token':item.unit==='call'?'每次':esc(item.unit)}</td></tr>`))}
    ${['estimated','mixed'].includes(row.usage_source)?'<p>含本地估算：按已观察到的文本分词，缺少缓存数据时按普通输入单价计费；未推算不可见的输出和推理。</p>':''}
    ${attempts.length?table(['尝试','请求状态','用量依据'],attempts.map(a=>`<tr><td>${esc(a.number)}</td><td>HTTP ${esc(a.status)}</td><td>${esc(({official:'官方用量',estimated:'本地估算',mixed:'官方用量 + 本地估算',none:'无计费用量'})[a.billing?.usage_source]||'未知')}${a.billing?.state==='pending'?' · 待核算':''}</td></tr>`)):''}
    <p>官方基准金额：${row.official_cost_microusd===null?'未提供':money(row.official_cost_microusd)}。按未舍入金额乘实际倍率，最后一次向上取整到 0.000001 USD。官方基准不是商户实际采购成本。</p>
    <details><summary>查看原始计费记录</summary><pre>${esc(JSON.stringify({usage:event.usage,cache_usage:event.cache_usage,billing:event.billing,merchant_pricing:quote},null,2))}</pre></details>
    <div class="actions">${btn('关闭','close')}</div>`;dialog.showModal();
}
