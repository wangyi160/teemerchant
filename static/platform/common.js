"use strict";
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>'$'+(Number(v)/1e6).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:6});
const date=v=>v?new Date(v*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const labels={pending:'待审核',provisioning:'等待 Router 同步',approved:'已开通',rejected:'未通过',enabled:'启用',disabled:'停用'};
const badge=(s,label)=>`<span class="badge ${esc(s)}">${esc(label||labels[s]||s)}</span>`;
const button=(title,action,id='',cls='')=>`<button type="button" class="${cls}" data-action="${action}" data-id="${esc(id)}">${esc(title)}</button>`;
const workspacePath=()=>'/account';
let csrf='';
function toast(text){
  $('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toast.timer);
  toast.timer=setTimeout(()=>$('#toast').classList.remove('show'),5000);
}
async function api(path,method='GET',data,operationId=newOperationId()){
  const response=await fetch('/account-api/'+path,{method,credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=local-csrf]').content,'X-Account-Mode':document.body.dataset.mode||'account','X-Merchant-ID':document.querySelector('meta[name=merchant-id]').content,'Idempotency-Key':operationId},
    body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){
    if(response.status===401){document.body.replaceChildren();parent.postMessage({type:'merchant-login-required'},location.origin);}
    if(response.status===401&&document.body.dataset.role&&document.body.dataset.mode!=='merchant')location.replace('/account/login');
    const error=Error(result.detail||result.error||'操作失败');error.status=response.status;throw error;
  }
  if(result.local_merchant_id){
    const identity=document.querySelector('meta[name=merchant-id]');
    const changed=identity.content!==result.local_merchant_id;
    identity.content=result.local_merchant_id;
    if(changed||path==='login'||path==='register')parent.postMessage({type:'merchant-bound'},location.origin);
  }
  return result;
}
function table(heads,rows,empty='暂无记录'){
  return `<div class="scroll"><table><thead><tr>${heads.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.join(''):`<tr><td colspan="${heads.length}" class="empty">${empty}</td></tr>`}</tbody></table></div>`;
}
function downloads(role){
  const apps=[
    {name:'teeproxy',role:'user',label:'用户客户端',description:'连接 Router，为 Codex、Claude 等 agent 提供经过 TEE 验证的本地 API。'},
    {name:'teemerchant',role:'merchant',label:'商户工作台',description:'Windows / Linux 商户服务，可远程管理账号池、授权、倍率和收入。'}
  ];
  return `<section class="software-downloads" aria-label="软件下载"><h2>软件下载</h2><p class="hint">Windows x64 · 在 GitHub 发布页获取最新版本</p><div class="download-grid">${apps.filter(app=>!role||app.role===role).map(app=>`<article class="download-card"><h3>${app.name}<span>${app.label}</span></h3><p>${app.description}</p><a class="download-link" href="https://github.com/wangyi160/${app.name}/releases/latest" target="_blank" rel="noopener noreferrer">下载 ${app.name} <span aria-hidden="true">↗</span></a></article>`).join('')}</div></section>`;
}
