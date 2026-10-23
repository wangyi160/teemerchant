"use strict";
let withdrawalData=null,withdrawalError='',withdrawalBusy=false,withdrawalAttempt=null;
let withdrawalBefore='',withdrawalPages=[];
const withdrawalNames={pending:'待处理',processing:'打款处理中',paid:'已打款',rejected:'已拒绝',cancelled:'已撤销'};
const withdrawalEventNames={apply:'提交申请',process:'开始处理',paid:'登记已打款',reject:'拒绝申请',cancel:'商户撤销'};
const withdrawalAmount=n=>{const v=BigInt(n||0);return (v/1000000n)+'.'+(v%1000000n).toString().padStart(6,'0').replace(/0+$/,'').padEnd(2,'0');};
const withdrawalRate=bps=>(Number(bps)/100)+'%';
const withdrawalBreakdown=r=>`<p>申请金额：${withdrawalAmount(r.amount_microusd)} USDT</p><p>手续费（${withdrawalRate(r.fee_rate_bps)}）：${withdrawalAmount(r.fee_microusd)} USDT</p><p>实际到账：<strong>${withdrawalAmount(r.payout_microusd)} USDT</strong></p>`;
function withdrawalPolicyView(p){
  return `<p>最低提现 ${withdrawalAmount(p.minimum_microusd)} USDT · 按所在档位对整笔申请金额计费</p>${table(['申请金额 · USDT','手续费率'],p.tiers.map(t=>`<tr><td>≥ ${withdrawalAmount(t.from_microusd)}${t.to_microusd===null?'':` 且 < ${withdrawalAmount(t.to_microusd)}`}</td><td>${withdrawalRate(t.rate_bps)}</td></tr>`))}`;
}
async function loadWithdrawals(){
  try{withdrawalData=await api('withdrawals'+(withdrawalBefore?'?before='+encodeURIComponent(withdrawalBefore):''));withdrawalError='';}
  catch(e){withdrawalError=e.message;}
}
function withdrawalsView(){
  const d=withdrawalData;
  let html='<section class="panel"><div class="heading"><div><h2>收入提现</h2><p>人工审核打款 · 1 USD = 1 USDT</p></div></div>';
  if(withdrawalError)html+=`<div class="notice warn">${esc(withdrawalError)}</div>`;
  if(!d)return html+'<p class="muted">正在读取提现记录…</p></section>';
  const available=BigInt(d.balance.available_microusd),canApply=d.can_apply&&available>=BigInt(d.fee_policy.minimum_microusd);
  html+=`<div class="cards withdrawal-cards">${[['可提现收入',d.balance.available_microusd],['申请中金额',d.balance.reserved_microusd],['累计已提现（含手续费）',d.balance.paid_microusd]].map(([label,n],i)=>`<div class="card"><small>${label} · USD</small><strong>${financeMoney(n)}</strong>${i===2?`<span class="sub">实际到账 ${withdrawalAmount(d.balance.paid_payout_microusd)} USDT · 手续费 ${withdrawalAmount(d.balance.paid_fee_microusd)} USDT</span>`:''}</div>`).join('')}</div>`;
  if(!d.can_apply)html+='<p class="muted">商户审核通过后可申请提现。</p>';
  else html+=`<div id="withdrawal-fee-tiers">${withdrawalPolicyView(d.fee_policy)}</div><form id="withdrawal-form" class="withdrawal-form"><label>提现通道<select name="network">${d.channels.map(n=>`<option value="${esc(n.id)}">${esc(n.name)} · ${esc(n.asset)}</option>`).join('')}</select></label><label>申请金额（含手续费）· USDT<input name="amount" type="number" required min="${withdrawalAmount(d.fee_policy.minimum_microusd)}" max="${withdrawalAmount(available>1000000000000n?1000000000000n:available>0n?available:0n)}" step="0.000001" inputmode="decimal"></label><label class="withdrawal-address">收款地址<input name="address" required maxlength="80" autocomplete="off" spellcheck="false"></label><div id="withdrawal-quote" class="withdrawal-address" aria-live="polite"></div><div class="actions"><button type="submit" class="primary" ${!canApply||withdrawalBusy?'disabled':''}>提交提现申请</button></div><div class="error" role="alert"></div></form>`;
  html+='</section>';
  return html+`<section class="panel"><h2>提现记录</h2>${table(['申请编号 / 时间','通道 / 收款地址','申请 / 手续费 / 到账 · USDT','状态','操作'],d.data.map(r=>`<tr><td>#${esc(r.id)}<span class="sub">${date(r.created_at)}</span></td><td>${esc(r.network_name)}<span class="sub withdrawal-address-text">${esc(r.address)}</span></td><td>申请 ${withdrawalAmount(r.amount_microusd)}<span class="sub">手续费（${withdrawalRate(r.fee_rate_bps)}）${withdrawalAmount(r.fee_microusd)}</span><span class="sub">到账 ${withdrawalAmount(r.payout_microusd)}</span></td><td>${esc(withdrawalNames[r.state])}${r.note?`<span class="sub">${esc(r.note)}</span>`:''}</td><td>${button('查看','withdrawal-detail',r.id)}${r.state==='pending'?button('撤销','withdrawal-cancel',r.id):''}</td></tr>`),'暂无提现申请。')}<div class="actions"><button data-action="withdrawal-prev" ${withdrawalPages.length?'':'disabled'}>上一页</button><button data-action="withdrawal-next" ${d.has_more?'':'disabled'}>下一页</button></div></section>`;
}
function bindWithdrawals(){
  const form=$('#withdrawal-form');if(!form)return;
  const amount=form.elements.amount,quote=$('#withdrawal-quote');
  const updateQuote=()=>{
    const policy=withdrawalData.fee_policy;
    const match=/^(\d+)(?:\.(\d{1,6}))?$/.exec(amount.value);
    if(!match){quote.textContent=`最低提现 ${withdrawalAmount(policy.minimum_microusd)} USDT，手续费从申请金额中扣除`;return;}
    const gross=BigInt(match[1])*1000000n+BigInt((match[2]||'').padEnd(6,'0'));
    if(gross<BigInt(policy.minimum_microusd)){quote.textContent=`未达到最低提现金额 ${withdrawalAmount(policy.minimum_microusd)} USDT`;return;}
    if(!amount.validity.valid){quote.textContent='请填写可提现范围内的有效金额';return;}
    const tier=policy.tiers.find(t=>t.to_microusd===null||gross<BigInt(t.to_microusd)),rate=tier.rate_bps,fee=(gross*BigInt(rate)+5000n)/10000n;
    quote.textContent=`手续费（${withdrawalRate(rate)}）：${withdrawalAmount(fee)} USDT · 实际到账：${withdrawalAmount(gross-fee)} USDT`;
  };
  amount.addEventListener('input',updateQuote);updateQuote();
  form.onsubmit=async event=>{
    event.preventDefault();if(withdrawalBusy)return;
    const data=Object.fromEntries(new FormData(form));data.address=data.address.trim();
    const body=JSON.stringify(data);if(!withdrawalAttempt||withdrawalAttempt.body!==body)withdrawalAttempt={body,id:newOperationId()};
    data.policy_version=withdrawalData.fee_policy.version;
    withdrawalBusy=true;form.querySelector('button').disabled=true;form.querySelector('.error').textContent='';
    try{await api('withdrawals','POST',data,withdrawalAttempt.id);withdrawalAttempt=null;withdrawalBefore='';withdrawalPages=[];await loadFinance(false);render();toast('提现申请已提交');}
    catch(e){
      form.querySelector('.error').textContent=e.message;
      // Refresh the quote without losing the address, amount, or retry identity.
      await loadWithdrawals();
      if(withdrawalData){
        amount.min=withdrawalAmount(withdrawalData.fee_policy.minimum_microusd);
        const available=BigInt(withdrawalData.balance.available_microusd);
        amount.max=withdrawalAmount(available>1000000000000n?1000000000000n:available>0n?available:0n);
        $('#withdrawal-fee-tiers').innerHTML=withdrawalPolicyView(withdrawalData.fee_policy);updateQuote();
      }
    }
    finally{withdrawalBusy=false;const b=$('#withdrawal-form button');if(b)b.disabled=!withdrawalData?.can_apply||BigInt(withdrawalData.balance.available_microusd)<BigInt(withdrawalData.fee_policy.minimum_microusd);}
  };
}
async function withdrawalAction(action,id){
  if(action==='withdrawal-next'&&withdrawalData.next_before){withdrawalPages.push(withdrawalBefore);withdrawalBefore=withdrawalData.next_before;await loadWithdrawals();render();}
  if(action==='withdrawal-prev'&&withdrawalPages.length){withdrawalBefore=withdrawalPages.pop();await loadWithdrawals();render();}
  if(action==='withdrawal-detail'){
    const r=await api('withdrawals/'+encodeURIComponent(id));
    modal('提现申请 #'+r.id,`<p>${esc(withdrawalNames[r.state])}</p>${withdrawalBreakdown(r)}<p>${esc(r.network_name)} · ${esc(r.asset)}</p><code class="withdrawal-address-text">${esc(r.address)}</code>${r.transaction_url?`<p><a href="${esc(r.transaction_url)}" target="_blank" rel="noopener noreferrer">查看交易 ↗</a></p><code class="withdrawal-address-text">${esc(r.transaction_id)}</code>`:''}${table(['时间','处理记录','说明'],r.events.map(e=>`<tr><td>${date(e.created_at)}</td><td>${esc(withdrawalEventNames[e.action])}</td><td>${esc(e.note||'—')}</td></tr>`))}`,null);
  }
  if(action==='withdrawal-cancel'){
    const r=await api('withdrawals/'+encodeURIComponent(id));
    modal('撤销提现申请 #'+r.id,`<p>${withdrawalAmount(r.amount_microusd)} USDT · ${esc(r.network_name)}</p>`,async(_,operationId)=>{await api('withdrawals/'+encodeURIComponent(id)+'/cancel','POST',{version:r.version},operationId);});
  }
}
setInterval(async()=>{
  if(!state||page!=='finance'||document.hidden||withdrawalBusy||$('#dialog').open||document.activeElement?.closest('form'))return;
  if(withdrawalData?.data.some(r=>['pending','processing'].includes(r.state))){await loadFinance(false);render();}
},15000);
