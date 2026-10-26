"use strict";
let recordPage={before:null,cursors:[],next:null,hasMore:false,error:'',loadedAt:0},recordRequest=null;
let platformRecordsRequest=null,platformRecordsAttempt=0;
function resetRecordPage(){records=[];recordPage={before:null,cursors:[],next:null,hasMore:false,error:'',loadedAt:0};recordRequest=null;platformRecordsRequest=null;platformRecordsAttempt=0;}
function recordsView(){
  const p=recordPage;
  return heading(names[section],'管理平台记录')+(p.error?`<p class="error">${esc(p.error)}${p.loadedAt?' · 以下为上次查询结果':''}</p>`:'')+
    `<section class="panel">${!p.loadedAt&&!p.error?MerchantLoading.placeholder():table(['ID','用户','模型 / 来源','详情'],records.map((r,i)=>{const e=r.event||r;return `<tr><td>${esc(r.seq??r.id)}</td><td>${esc(e.user_id||'')}</td><td>${esc(e.model||e.provider||'')}</td><td>${btn('查看','record',String(i))}</td></tr>`}))}
    <div class="actions"><span>第 ${p.cursors.length+1} 页</span><button data-action="records-page" data-id="-1" ${!p.cursors.length||recordRequest?'disabled':''}>上一页</button><button data-action="records-page" data-id="1" ${!p.hasMore||recordRequest?'disabled':''}>下一页</button></div></section>`+
    (p.loadedAt?`<p>查询时间 ${esc(new Date(p.loadedAt).toLocaleString())}</p>`:'');
}
async function loadRecords(move=0){
  if(recordRequest||!state?.auth?.authorized||!['usage','refusals'].includes(section))return;
  if(move===1&&!recordPage.hasMore||move===-1&&!recordPage.cursors.length)return;
  const before=move===1?recordPage.next:move===-1?recordPage.cursors.at(-1):recordPage.before;
  const epoch=dataEpoch,target=section,attempt={};recordRequest=attempt;platformRecordsAttempt=Date.now();
  try{
    const result=await api('/api/records/'+target+(before?'?before='+encodeURIComponent(before):''));
    if(epoch!==dataEpoch||section!==target||recordRequest!==attempt)return;
    const cursors=move===1?[...recordPage.cursors,recordPage.before]:move===-1?recordPage.cursors.slice(0,-1):recordPage.cursors;
    records=result.data;recordPage={before,cursors,next:result.next_before,hasMore:result.has_more,error:'',loadedAt:Date.now()};
  }catch(error){if(epoch===dataEpoch&&section===target&&recordRequest===attempt)recordPage.error=error.message;}
  finally{if(recordRequest===attempt){recordRequest=null;if(epoch===dataEpoch&&section===target&&!$('#dialog').open)render();}}
}
async function refreshPlatformRecords(force=false){
  if(platformRecordsRequest||!state?.auth?.authorized||!['usage','refusals','revenue'].includes(section)||$('#dialog').open)return;
  if(!force&&(document.activeElement?.closest('form')||Date.now()-platformRecordsAttempt<60000))return;
  const attempt={};platformRecordsRequest=attempt;platformRecordsAttempt=Date.now();
  const target=section,epoch=dataEpoch,navigation=pageEpoch;
  try{
    if(target==='revenue'){try{await loadRevenue();}catch{}if(epoch===dataEpoch&&navigation===pageEpoch&&section===target&&!$('#dialog').open)render();}
    else await loadRecords();
  }finally{if(platformRecordsRequest===attempt)platformRecordsRequest=null;}
}
