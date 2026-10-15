"use strict";
let revenueData = null;
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
  billing_observer_unavailable:'计费观察数据不可用'
};

function pricingView() {
  return heading('费率与限速','默认倍率用于新建账号池；实际计费始终以账号池保存的倍率为准。') +
    `<section class="panel"><div class="panelhead"><h2>新建账号池的默认倍率</h2>${btn('修改默认倍率','default-price')}</div>
      <div class="default-rate">${usd(state.settings.default_multiplier_millionths ?? 1000000)}×</div>
      <p>只作为新建账号池时的初始值。修改默认值不会调整已有账号池，也不会改变历史账单。</p></section>
    <section class="panel"><div class="panelhead"><h2>账号池实际倍率</h2></div>
    ${table(['账号池','服务','实际倍率','操作'],pools().map(p=>{
      const rate=(settings().pool_pricing||[]).find(r=>r.pool===p.name);
      return `<tr><td><strong>${esc(p.name)}</strong></td><td>${esc(p.provider)}</td><td>${rate?usd(rate.multiplier_millionths)+'×':'未设置'}</td><td>${btn('前往账号池','pool-price',p.name)}</td></tr>`;
    }))}<p>在“账号与模型”中编辑实际倍率。调用开始时锁定官方报价和池倍率；输入、输出及缓存费用按这份历史快照计算。Workbuddy 使用测试基准。</p></section>
    <section class="panel"><div class="panelhead"><h2>商户总限速</h2>${btn('调整限速','limits')}</div><p>并发 ${settings().max_concurrent} · RPM ${settings().rpm||'不限'} · TPM ${settings().tpm||'不限'}</p></section>`;
}

function pricingDefaultsForm() {
  form('默认倍率',field('新建账号池初始倍率','multiplier',String((state.settings.default_multiplier_millionths ?? 1000000)/1e6),'text','required inputmode="decimal"')+
    '<p>已有账号池仍使用各自的实际倍率。此默认值保存在商户本机，不参与 Router 计费。</p>',async f=>{
      const result=await api('/api/pricing-defaults','PUT',{default_multiplier_millionths:amount(f.get('multiplier').trim())});
      state.settings.default_multiplier_millionths=result.default_multiplier_millionths;render();
    },{success:'默认倍率已保存；已有账号池保持原倍率'});
}

async function loadRevenue() {
  revenueData=await api('/api/revenue?'+new URLSearchParams(revenueQuery));
}

function revenueView() {
  if(!revenueData)return heading('收入','正在读取本地归档…');
  const r=revenueData,s=r.summary;
  const card=(label,value,note)=>`<div class="card"><small>${label}</small><strong>${value}</strong><small>${note}</small></div>`;
  const rows=r.data.map((row,i)=>`<tr>
    <td>${esc(revenueTime(row.created_at))}<small>${btn('查看账单','revenue-detail',String(i))}</small></td>
    <td>${esc(row.pool||'未选池')}<small>${esc(row.model)}</small></td>
    <td class="revenue-user">${esc(row.user_id||'未知')}</td>
    <td>${row.multiplier_millionths===null?'—':usd(row.multiplier_millionths)+'×'}</td>
    <td class="money">${money(row.charge_microusd)}</td>
    <td><span class="${row.state==='pending'?'error':'badge'}">${row.state==='pending'?'待核算':row.basis==='not_forwarded'?'未转发 · 不计费':'已计费'}</span><small>HTTP ${row.status??'—'}${row.state==='pending'?'<br>'+esc(revenueReasons[row.reason]||row.reason):''}</small></td>
  </tr>`);
  return heading('收入','根据请求发生时的官方报价和账号池实际倍率，汇总已归档的应收费用。',btn('同步账单','sync','','primary'))+
    `<div class="cards revenue-cards">
    ${card('累计应收 · USD',money(s.charge_microusd),`${s.calculated} 笔金额已确认`)}
    ${card('今日应收 · USD',money(s.today_microusd),'按商户本机日期')}
    ${card('本月应收 · USD',money(s.month_microusd),'按商户本机日期')}
    ${card('待核算请求',s.pending,'未计入应收合计')}
    </div><p class="revenue-scope">以上统计全部本地归档，共 ${s.requests} 笔；不是最近 200 条的合计。应收表示调用费用，实际到账与结算尚未接入。币种 USD，不进行汇率换算。</p>
    <section class="panel"><form id="revenue-filter" class="revenue-filter">
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
    <p>本地归档更新时间以“同步账单”为准；查询生成于 ${esc(revenueTime(r.generated_at))}（${esc(r.timezone)}）。待核算不表示免费，补齐计费依据前不会猜算收入。</p>`;
}

function bindRevenueFilters() {
  const form=$('#revenue-filter');if(!form)return;
  form.querySelector('[data-action=revenue-reset]').type='button';
  form.onsubmit=async e=>{
    e.preventDefault();const data=new FormData(form),previous={...revenueQuery};
    revenueQuery={start:data.get('start'),end:data.get('end'),pool:data.get('pool'),offset:'0'};
    const button=form.querySelector('[type=submit]');button.disabled=true;
    try{await loadRevenue();render();}catch(error){revenueQuery=previous;toast(error.message);}
    finally{button.disabled=false;}
  };
}

async function revenueDetail(index) {
  const row=revenueData.data[Number(index)];if(!row)return;
  const record=await api('/api/revenue/record?'+new URLSearchParams({instance:row.instance,seq:row.seq}));
  const event=record.event||{},bill=event.billing||{},quote=event.merchant_pricing||{};
  const items=Array.isArray(bill.items)?bill.items.filter(item=>item&&typeof item==='object'):[];
  const kinds={input:'输入',output:'输出',cache_read:'缓存读取',cache_write:'缓存写入',cache_write_hour:'缓存写入（1 小时）'};
  const dialog=$('#dialog');dialog.classList.add('wide');
  dialog.innerHTML=`<h2>调用账单</h2><div class="panelhead"><span>${esc(row.pool)} / ${esc(row.model)}</span><strong class="money">应收 ${money(row.charge_microusd)}</strong></div>
    <p>${esc(revenueTime(row.created_at))} · HTTP ${row.status??'—'} · ${row.multiplier_millionths===null?'倍率未知':usd(row.multiplier_millionths)+'×'}<br>
    ${row.state==='pending'?esc(revenueReasons[row.reason]||row.reason):row.basis==='legacy_quote'?'根据请求时的历史报价计算':row.basis==='not_forwarded'?'请求未转发，不计费':'Router 已计算，使用请求时的价格快照'}<br>
    用户：${esc(row.user_id)}<br>请求：<code>${esc(row.request_id)}</code><br>
    商户价格版本 ${esc(row.price_version??'—')} · 官方价格版本 ${esc(row.official_price_revision??'—')}</p>
    ${table(['计费项目','用量','官方单价 · USD','单位'],items.map(item=>`<tr><td>${esc(kinds[item.kind]||item.kind)}</td><td>${esc(item.quantity)}</td><td class="money">${money(item.unit_price_microusd)}</td><td>${item.unit==='million_tokens'?'每百万 token':item.unit==='call'?'每次':esc(item.unit)}</td></tr>`))}
    <p>官方基准金额：${row.official_cost_microusd===null?'未提供':money(row.official_cost_microusd)}。按未舍入金额乘实际倍率，最后一次向上取整到 0.000001 USD。官方基准不是商户实际采购成本。</p>
    <details><summary>查看原始计费记录</summary><pre>${esc(JSON.stringify({usage:event.usage,cache_usage:event.cache_usage,billing:event.billing,merchant_pricing:quote},null,2))}</pre></details>
    <div class="actions">${btn('关闭','close')}</div>`;dialog.showModal();
}
