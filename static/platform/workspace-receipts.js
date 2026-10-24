"use strict";
let ownedReceipts=null,ownedReceiptError='',ownedReceiptRequest=null,ownedReceiptAttempt=0;
let ownedReceiptCursors=[null],ownedReceiptPage=0,ownedReceiptDetail=null;

async function loadOwnedReceipts(redraw=true,move=0,reset=false){
  if(ownedReceiptRequest&&!reset)return ownedReceiptRequest;
  const attempt=++ownedReceiptAttempt;
  const target=reset?0:Math.max(0,ownedReceiptPage+move);
  const cursor=reset?null:move===1?ownedReceipts?.next_before:ownedReceiptCursors[target];
  if(move===1&&(!ownedReceipts?.has_more||cursor==null))return;
  ownedReceiptError='';
  ownedReceiptRequest=(async()=>{
    try{
      const result=await api('receipts/mine'+(cursor==null?'':'?before='+encodeURIComponent(cursor)));
      if(attempt!==ownedReceiptAttempt)return;
      // Keep the last successful page and cursor on a failed request.
      ownedReceipts=result;ownedReceiptPage=target;
      if(reset)ownedReceiptCursors=[null];
      else if(move===1){ownedReceiptCursors=ownedReceiptCursors.slice(0,target);ownedReceiptCursors.push(cursor);}
    }catch(error){if(attempt===ownedReceiptAttempt)ownedReceiptError=error.message;}
    finally{if(attempt===ownedReceiptAttempt){ownedReceiptRequest=null;if(redraw&&page==='receipts')render();}}
  })();
  return ownedReceiptRequest;
}

function ownedReceiptsView(){
  const merchant=workspace.role==='merchant',data=ownedReceipts;
  let html=`<div class="heading"><div><h1>消费凭据</h1><p>${merchant?'本商户':'本人'}已存证的模型调用流水与签名凭据。</p></div><a class="button-link" href="https://github.com/wangyi160/teerouter/releases/tag/receipt-verifier-v0.1.2" target="_blank" rel="noopener noreferrer">开源校验器 ↗</a></div>`;
  if(ownedReceiptError)html+=`<div class="notice warn" role="alert">凭据读取失败：${esc(ownedReceiptError)}。将自动重试。</div>`;
  if(!data)return html+(ownedReceiptError?'':'<p class="loading" role="status">正在读取消费凭据…</p>');
  return html+`<section class="panel finance-records"><h2>已存证流水</h2>${table(['流水号','账号池 / 模型','时间','金额 · USD','操作'],data.data.map(row=>`<tr><td>#${esc(row.seq)}</td><td>${esc(row.pool||'—')}<span class="sub">${esc(row.provider)} / ${esc(row.model)}</span></td><td>${date(row.created_at)}</td><td>${row.charge_microusd==null||row.charge_microusd===''?'待核算':financeMoney(row.charge_microusd)}</td><td>${button('查看凭据','receipt-detail',row.seq,'link')}</td></tr>`),merchant?'本商户暂无已存证的调用流水。':'暂无已存证的消费流水。')}
    <div class="finance-pagination"><span>第 ${ownedReceiptPage+1} 页 · 本页 ${data.data.length} 条</span><div class="actions">
      <button type="button" data-action="receipt-first" ${!ownedReceiptPage||ownedReceiptRequest?'disabled':''}>最新</button>
      <button type="button" data-action="receipt-prev" ${!ownedReceiptPage||ownedReceiptRequest?'disabled':''}>上一页</button>
      <button type="button" data-action="receipt-next" ${!data.has_more||ownedReceiptRequest?'disabled':''}>下一页</button></div></div></section>`;
}

function bindOwnedReceipts(currentPage){
  if(currentPage==='receipts'&&!ownedReceipts&&!ownedReceiptError&&!ownedReceiptRequest)loadOwnedReceipts();
}

async function ownedReceiptAction(action,id){
  if(action==='receipt-first')return loadOwnedReceipts(true,0,true);
  if(action==='receipt-prev')return loadOwnedReceipts(true,-1);
  if(action==='receipt-next')return loadOwnedReceipts(true,1);
  if(action==='receipt-detail'){
    if(!/^\d{1,19}$/.test(id))return;
    const data=await api('receipts/records/'+id),manifest=JSON.parse(data.batch.manifest);
    if(page!=='receipts')return;
    const fields=[['流水编号','#'+id],['所属批次','#'+manifest.batch_no],['批次摘要',data.batch.batch_hash],
      ['上一批摘要',manifest.previous_batch_hash||'无'],['签名公钥',manifest.public_key],['Ed25519 签名',data.batch.signature],
      [manifest.records_sha256?'流水摘要':'流水树根',manifest.records_sha256||manifest.merkle_root]];
    ownedReceiptDetail={id,data};
    modal('消费凭据 #'+id,`<div class="owned-receipt-download">${button('下载凭据 JSON','receipt-download',id,'primary')}</div>
      <dl class="owned-receipt-fields">${fields.map(([key,value])=>`<dt>${esc(key)}</dt><dd><code>${esc(value)}</code></dd>`).join('')}</dl>
      <details><summary>查看签名原文与证明</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>`,null);
  }
  if(action==='receipt-download'&&ownedReceiptDetail?.id===id){
    const url=URL.createObjectURL(new Blob([JSON.stringify(ownedReceiptDetail.data,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='receipt-'+id+'.json';
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
}

$('#dialog').addEventListener('close',()=>{ownedReceiptDetail=null;});
setInterval(()=>{
  if(document.body.dataset.mode==='merchant')return;
  if(state&&page==='receipts'&&ownedReceiptPage===0&&!document.hidden&&!$('#dialog').open)loadOwnedReceipts();
},300000);
