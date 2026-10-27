"use strict";
const $=s=>document.querySelector(s), esc=v=>String(v??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={overview:"概览",accounts:"账号与模型",customers:"客户授权",revenue:"收入",receipts:"消费凭据",usage:"调用记录",refusals:"拒答记录",connection:"账号与连接"};
let state=null, section=location.hash.startsWith('#connection')?'connection':'overview', records=[];
let dataEpoch=0,pageEpoch=0;
const accessScope=s=>[s?.auth?.scope||'',!!s?.auth?.authorized,s?.enrollment.id,s?.settings.platform_url].join('|');
function clearMerchantView(){
  dataEpoch++;pageEpoch++;records=[];selectedAccountPool=null;poolModelEditor=null;
  resetRecordPage();
  revenueData=null;revenueError='';revenueLoadedAt=0;revenueQuery={start:'',end:'',pool:'',offset:'0'};
  const dialog=$('#dialog');dialog.close();dialog.innerHTML='';delete dialog.dataset.busy;
  $('#toast').textContent='';$('#toast').classList.remove('show');
}
function acceptState(next){
  const changed=!!state&&accessScope(state)!==accessScope(next);
  if(changed){clearMerchantView();resetConnectionAccount();}
  state=next;
  if(changed||!connectionAccount)connectionAccount=next.auth?.view||null;
  if(connectionAccount)connectionLoaded=connectionScope();
  return changed;
}
function hideMerchantView(){
  clearMerchantView();resetConnectionAccount();
  state={...state,settings:{platform_url:state.settings.platform_url},auth:{authenticated:false,authorized:false,accounts:[]},remote:null,identity:{},profiles:[],credentials:{saved:false},connected:false,error:'',archive:{}};
  section='connection';connectionMode=null;render();
}
const btn=(label,action,id="",cls="")=>`<button class="${cls}" data-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
const field=(label,name,value="",type="text",attrs="")=>`<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${attrs}></label>`;
const table=(heads,rows)=>`<div class="scroll"><table><thead><tr>${heads.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.length?rows.join(""): `<tr><td class="empty" colspan="${heads.length}">暂无记录</td></tr>`}</tbody></table></div>`;
const remote=()=>state.remote, settings=()=>remote()?.settings, pools=()=>remote()?.pools||[];
const usd=n=>(n/1e6).toLocaleString("en-US",{maximumFractionDigits:6});
function amount(v){if(!/^\d+(\.\d{1,6})?$/.test(v))throw Error("倍率最多保留 6 位小数");const [a,b=""]=v.split('.');const n=Number(a)*1e6+Number(b.padEnd(6,'0'));if(!Number.isSafeInteger(n)||n>1e15)throw Error("倍率超出范围");return n;}
async function api(path,method="GET",body){
  const finish=method==='GET'||['/api/refresh','/api/connect','/api/accounts/refresh'].includes(path)?MerchantLoading.begin():()=>{};
  try{
  const epoch=dataEpoch;
  const identity=path==='/api/state'?undefined:state?.enrollment.id;
  let value;
  if(window.teemerchant){
    const r=await window.teemerchant.request(method,path,body,identity);
    if(!r.ok){if(r.status===401&&epoch===dataEpoch){hideMerchantView();}throw Error(r.error);}value=r.value;
  }else{
    const headers={"Content-Type":"application/json","X-CSRF-Token":$('meta[name=csrf]').content};
    if(identity)headers['X-Merchant-ID']=identity;
    const r=await fetch(path,{method,headers,cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)});
    value=await r.json();if(!r.ok)throw Error(value.error||"操作失败");
  }
  if(epoch!==dataEpoch)throw Error('账号状态已变化，请重新操作');
  if(identity&&state?.enrollment.id!==identity)throw Error('商户已切换，请重新操作');
  return value;
  }finally{finish();}
}
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').classList.remove('show'),6000);}
async function reload(){
  const next=await api('/api/state');
  if(!state&&!next.credentials?.saved)section='connection';
  acceptState(next);render();
}
async function enterSection(id){
  if(id==='finance')id='revenue';
  section=id;
  const navigation=++pageEpoch,epoch=dataEpoch;
  resetRecordPage();
  revenueRequest=null;
  if(id==='revenue'){revenueData=null;revenueError='';revenueLoadedAt=0;}
  history.replaceState(null,'',id==='connection'?'#connection':location.pathname);
  render();
  if(id==='connection')return loadConnectionAccount(true);
  if(!state.auth?.authorized)return;
  if(['usage','refusals','revenue'].includes(id))return refreshPlatformRecords(true);
  // Embedded platform pages query on their own initial load.
  if(['finance','receipts'].includes(id))return;
  if(state.auth?.authorized)await api('/api/merchant');
  const next=await api('/api/state');
  if(navigation!==pageEpoch||epoch!==dataEpoch)return;
  acceptState(next);
  if(!$('#dialog').open&&!(id==='accounts'&&poolModelEditor)&&!document.activeElement?.closest('form'))render();
}
function draft(){return AccountConfig.draft(remote());}
async function save(value){const result=await api('/api/config','PUT',value);state.remote=result;state.settings.minimum_revision=result.settings.revision;render();return result;}
function form(title,html,action,options={}){
  const dialog=$('#dialog');dialog.classList.remove('wide');delete dialog.dataset.busy;
  dialog.innerHTML=`<h2>${esc(title)}</h2><form>${html}<p class="error" role="alert"></p><div class="actions">${btn('取消','close')}<button type="submit" class="primary">${esc(options.submit||'保存')}</button></div></form>`;
  dialog.querySelector('[data-action=close]').type='button';
  dialog.querySelector('form').onsubmit=async e=>{
    e.preventDefault();if(dialog.dataset.busy)return;
    const data=new FormData(e.target),controls=Array.from(e.target.elements),disabled=controls.map(c=>c.disabled);
    dialog.dataset.busy='true';controls.forEach(c=>c.disabled=true);
    try{await action(data);dialog.close();dialog.innerHTML='';toast(options.success||(state.remote?.local_sync?.state==='pending'?'已保存到本地，等待同步 Router':'已保存到本地并同步至 Router'));}
    catch(error){dialog.querySelector('.error').textContent=options.rawErrors?error.message:error.message.includes('local_revision_conflict')?'本地配置已变化，当前输入已保留。请重试保存。':error.message.includes('revision_conflict')?'Router 配置版本已变化，当前输入已保留。请重试保存。':error.message;}
    finally{delete dialog.dataset.busy;controls.forEach((c,i)=>c.disabled=disabled[i]);}
  };dialog.showModal();
}
function heading(title,desc,action=""){return `<div class="heading"><div><h1>${title}</h1><p>${desc}</p></div>${action}</div>`;}
function render(){if(!state)return;if(section==='revenue'&&state.auth?.authorized&&$('#income-page')?.dataset.epoch===String(pageEpoch)){renderIncome();updateRouterStatus();return;}$('#app').innerHTML=`<div class="shell"><aside><div class="brand">teemerchant<small>商户管理工作台</small></div>${Object.entries(names).map(([id,label])=>btn(label,'nav',id,section===id?'active':'')).join('')}<footer>商户独立身份 · 平台账务<br>经 TEE 验证后直连 Router</footer></aside><main><header><div><strong>${esc(names[section])}</strong><span class="status">${state.connected?esc(remote().merchant.name)+' · Router 已连接':'Router 尚未连接'}</span></div><div class="actions">${btn('刷新','reload')}</div></header><div class="content"><div id="router-connection-error" class="notice error" hidden></div>${view()}</div></main></div>`;bindPoolModelEditor();bindRevenueFilters();updateRouterStatus();}
function view(){if(section==='connection')return connection();if(!state.auth?.authorized)return heading(names[section],'')+`<section class="panel empty">${state.auth?.restoring?'正在恢复登录':state.auth?.authenticated?'当前账号尚未开通商户服务':'请先登录商户账号'}<div class="actions">${btn('账号与连接','nav','connection','primary')}</div></section>`;if(['finance','receipts'].includes(section))return `<iframe class="platform-frame" title="${esc(names[section])}" src="/account/${section}#${section}"></iframe>`;if(['usage','refusals'].includes(section))return recordsView();if(section==='revenue')return revenueView();if(!remote())return heading('连接你的 Router','')+`<section class="panel">${btn('账号与连接','nav','connection','primary')}</section>`;
if(section==='overview')return heading('商户概览','你的上游账号与客户访问状态。')+`<div class="cards"><div class="card"><small>账号池</small><strong>${pools().length}</strong></div><div class="card"><small>已使用客户</small><strong>${settings().customers.length}</strong></div><div class="card"><small>价格 / 配置版本</small><strong>v${settings().revision}</strong></div></div><section class="panel"><h2>${esc(remote().merchant.name)}</h2><p>商户 ID：<code>${esc(remote().merchant.id)}</code></p><p>用户沿用平台 API key；商户标识会出现在模型和账号池名称中。</p><div class="notice">平台资源上限：并发 ${remote().merchant.max_concurrent} · RPM ${remote().merchant.rpm||'不限'} · TPM ${remote().merchant.tpm||'不限'}。实际采用平台与商户配置中较低的限额。</div></section>`;
if(section==='accounts')return accountsView();
if(section==='customers')return customersView();

}
async function copyMerchantValue(value){
  if(window.teemerchant)await window.teemerchant.copyText(value);
  else await navigator.clipboard.writeText(value);
}

function customersView(){
  return heading('客户授权','',btn('默认客户权限','customer-defaults','','primary'))+
    `<section class="panel">${table(['平台用户 ID','状态','拒答次数 / 阈值','模型 / 账号池权限','并发 / RPM / TPM','操作'],settings().customers.map(c=>{
      const r=remote().customer_refusals?.[c.user_id];
      const status=!c.enabled?'停用':r?.blocked?'拒答超限 · 自动停用':'启用';
      return `<tr><td>${esc(c.user_id)}</td><td>${status}</td><td>${r?esc(r.count)+' / '+(r.limit||'不限'):'—'}</td><td>${esc(c.models.join(', ')||'全部本商户模型')}</td><td>${c.max_concurrent} / ${c.rpm||'不限'} / ${c.tpm||'不限'}</td><td>${btn('编辑','customer',c.user_id)}${r?.count?btn('清零并解除限制','reset-refusals',c.user_id):''}</td></tr>`;
    }))}</section>`;
}
async function resetRefusals(id){
  const merchant=state.enrollment.id;
  await api('/api/merchant');await reload();
  if(state.enrollment.id!==merchant)throw Error('商户已切换，请重新操作');
  const c=settings().customers.find(c=>c.user_id===id),r=remote().customer_refusals?.[id];
  if(!c||!r||!Number.isSafeInteger(r.total))throw Error('请刷新客户列表后重试');
  const d=draft();d.customers=[{...c,enabled:true,refusal_baseline:r.total}];
  await save(d);
}
function customerForm(id){
  const c=id?settings().customers.find(c=>c.user_id===id):settings().customer_defaults;
  if(!c)throw Error('请刷新客户列表后重试');
  form(id?'客户授权':'默认客户权限',(id?`<p>平台用户：<code>${esc(id)}</code></p>`:'<p>适用于首次调用及尚未单独设置权限的客户。</p>')+
    `<label><input type="checkbox" name="enabled" ${c.enabled?'checked':''}>允许调用</label>`+
    field('权限（逗号分隔；留空为本商户全部模型）','models',c.models.join(', '))+
    field('并发上限','max_concurrent',c.max_concurrent,'number','required min=1 max=4096')+
    field('RPM（0 为不限）','rpm',c.rpm,'number','required min=0')+
    field('TPM（0 为不限）','tpm',c.tpm,'number','required min=0')+
    field(id?'拒答阈值（留空继承默认；0 不限制）':'拒答阈值（0 不限制）','refusal_limit',c.refusal_limit??(id?'':0),'number','min=0 max=1000000000 step=1'+(id?'':' required')),async f=>{
      const limit=f.get('refusal_limit').trim();
      const d=draft(),item={enabled:f.has('enabled'),models:f.get('models').split(',').map(v=>v.trim()).filter(Boolean),max_concurrent:Number(f.get('max_concurrent')),rpm:Number(f.get('rpm')),tpm:Number(f.get('tpm')),refusal_limit:limit===''?null:Number(limit)};
      if(id)item.refusal_baseline=c.refusal_baseline||0;
      // Customer updates are patches. Unrelated edits must not freeze default
      // policies into individual overrides or erase customers discovered later.
      d.customers=id?[{user_id:id,...item}]:[];
      if(!id)d.customer_defaults=item;
      await save(d);
    });
}

async function action(a,id){
if(a==='upload-policy'){$('#policy-file-'+id)?.click();return;}
if(a.startsWith('identity-'))return connectionAction(a);
if(a==='merchant-switch'){
  const id=$('#merchant-profile').value;
  if(id===state.auth?.account?.identity_id&&state.auth?.authorized)return;
  await acceptConnectionAccount(await connectionApi('select','POST',{account_id:id}));toast('已切换账号');return;
}
if(a==='account-errors')return accountErrors(id);
if(a==='refresh-account'){await api('/api/accounts/refresh','POST',{id});return reload();}
if(a==='nav')return enterSection(id);
if(a==='close'){if($('#dialog').dataset.busy)return;$('#dialog').close();$('#dialog').innerHTML='';return;}if(a==='reload'){if(['usage','refusals','revenue'].includes(section))return refreshPlatformRecords(true);if(state.auth?.authorized)await api('/api/merchant');return reload();}if(a==='pool')return poolForm();if(a==='edit-pool')return poolForm(id);if(a==='edit-pool-models')return poolModelsForm(id);if(a==='account')return accountForm();if(a==='sync-config'){await api('/api/config/sync','POST',{});await reload();toast(remote().local_sync?.state==='pending'?'本地资料已保留，同步尚未完成':'本地配置已同步至 Router');return;}if(a==='retry-account'){await api('/api/accounts/retry','POST',{id});return reload();}if(a==='edit-account')return accountForm(id);if(a==='import-accounts')return batchAccountForm();if(a==='account-pool'){selectedAccountPool=id;return render();}if(a==='customer')return customerForm(id);if(a==='reset-refusals')return resetRefusals(id);if(a==='customer-defaults')return customerForm('');if(a==='revenue-detail')return revenueDetail(id);if(a==='revenue-page'){revenueQuery.offset=id;if(await loadRevenue())render();return;}if(a==='revenue-reset'){revenueQuery={start:'',end:'',pool:'',offset:'0'};if(await loadRevenue())render();return;}if(a==='refresh'){await api('/api/refresh','POST',{});return reload();}if(a==='toggle-account'){const d=draft(),a=d.pools.flatMap(p=>p.deployments).find(a=>a.id===id);a.enabled=!accountEnabled(a);if(a.oauth_account)d.accounts={[a.oauth_account]:{enabled:a.enabled}};return save(d);}if(a==='delete-account')return form('删除账号',`<p>删除账号 ${esc(id)} 及其凭证，历史记录保留。</p>`,async()=>{const d=draft();for(const p of d.pools){const a=p.deployments.find(a=>a.id===id);if(a){p.deployments=p.deployments.filter(a=>a.id!==id);if(a.oauth_account)d.delete_accounts=[a.oauth_account];else d.delete_secrets=[a.api_key_ref];}}await save(d);});if(a==='records-page')return loadRecords(Number(id));if(a==='record'){const dialog=$('#dialog');dialog.innerHTML=`<h2>记录详情</h2><pre>${esc(JSON.stringify(records[Number(id)],null,2))}</pre>${btn('关闭','close')}`;dialog.showModal();}}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;e.preventDefault();b.disabled=true;try{await action(b.dataset.action,b.dataset.id);}catch(error){toast(error.message);}finally{b.disabled=false;}});
document.addEventListener('submit',async e=>{if(e.target.id!=='connection')return;e.preventDefault();try{await api('/api/settings','PUT',Object.fromEntries(new FormData(e.target)));await reload();void connectRouterAutomatically();toast('连接设置已保存');}catch(error){toast(error.message);}});
document.addEventListener('submit',async e=>{if(e.target.id!=='platform-connection')return;e.preventDefault();try{await api('/api/platform','PUT',Object.fromEntries(new FormData(e.target)));resetConnectionAccount();await reload();loadConnectionAccount().catch(error=>toast(error.message));toast('管理平台地址已保存');}catch(error){toast(error.message);}});
$('#dialog').addEventListener('cancel',e=>{if($('#dialog').dataset.busy)e.preventDefault();});
$('#dialog').addEventListener('close',()=>{if(!$('#dialog').open)$('#dialog').innerHTML='';});
reload().then(()=>{void connectRouterAutomatically();if(connectionMode==='register')return connectionAction('identity-register');return enterSection(section);}).catch(e=>toast(e.message));
// Revalidate remembered logins after a restart/outage without asking for a password again.
let loginCheckAt=Date.now(),loginChecking=false;
setInterval(async()=>{
  if(loginChecking||!state?.auth?.authenticated||$('#dialog').open||document.activeElement?.closest('form'))return;
  if(!state.auth.restoring&&Date.now()-loginCheckAt<300000)return;
  loginChecking=true;
  try{
    const next=await api('/api/state');
    const changed=acceptState(next);
    if(changed||state.auth?.restoring){render();if(state.auth?.authorized)void connectRouterAutomatically();}
    loginCheckAt=Date.now();
  }catch{}finally{loginChecking=false;}
},15000);
// Reflect background model-policy cleanup without discarding an active form.
let catalogViewPolling=false;
setInterval(async()=>{
  if(catalogViewPolling||!state?.connected||section!=='accounts'||poolModelEditor||$('#dialog').open)return;
  catalogViewPolling=true;
  const current=state;
  try{
    const next=await api('/api/state');
    if(state!==current||section!=='accounts'||poolModelEditor||$('#dialog').open)return;
    if(accessScope(next)!==accessScope(current)){acceptState(next);render();return;}
    if(next.enrollment.id!==current.enrollment.id)return;
    if(next.remote?.local_revision!==current.remote?.local_revision||
        next.remote?.settings.revision!==current.remote?.settings.revision){state=next;render();}
  }catch{}finally{catalogViewPolling=false;}
},10000);

document.addEventListener('change',async e=>{
  if(!e.target.dataset.policy||!e.target.files?.length)return;
  const input=e.target,kind=input.dataset.policy;
  const button=$('#policy-button-'+kind);
  if(button)button.disabled=true;
  try{const file=input.files[0];if(file.size>2*1024*1024)throw Error('策略文件过大');
    const result=await api('/api/policy','POST',{kind,policy:JSON.parse(await file.text())});
    const form=input.closest('form');if(form)form.elements[kind+'_policy'].value=result.path;
  }catch(error){toast(error.message);}finally{input.value='';if(button)button.disabled=false;}
});
