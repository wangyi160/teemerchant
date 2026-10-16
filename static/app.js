"use strict";
const $=s=>document.querySelector(s), esc=v=>String(v??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={overview:"概览",accounts:"账号与模型",customers:"客户授权",limits:"限速",revenue:"\u6536\u5165",usage:"调用记录",refusals:"拒答记录",connection:"连接与身份"};
let state=null, section="overview", records=[];
const btn=(label,action,id="",cls="")=>`<button class="${cls}" data-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
const field=(label,name,value="",type="text",attrs="")=>`<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${attrs}></label>`;
const table=(heads,rows)=>`<div class="scroll"><table><thead><tr>${heads.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.length?rows.join(""): `<tr><td class="empty" colspan="${heads.length}">暂无记录</td></tr>`}</tbody></table></div>`;
const remote=()=>state.remote, settings=()=>remote()?.settings, pools=()=>remote()?.pools||[];
const usd=n=>(n/1e6).toLocaleString("en-US",{maximumFractionDigits:6});
function amount(v){if(!/^\d+(\.\d{1,6})?$/.test(v))throw Error("倍率最多保留 6 位小数");const [a,b=""]=v.split('.');const n=Number(a)*1e6+Number(b.padEnd(6,'0'));if(!Number.isSafeInteger(n)||n>1e15)throw Error("倍率超出范围");return n;}
async function api(path,method="GET",body){if(window.teemerchant){const r=await window.teemerchant.request(method,path,body);if(!r.ok)throw Error(r.error);return r.value;}const r=await fetch(path,{method,headers:{"Content-Type":"application/json","X-CSRF-Token":$('meta[name=csrf]').content},body:body===undefined?undefined:JSON.stringify(body)});const value=await r.json();if(!r.ok)throw Error(value.error||"操作失败");return value;}
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').classList.remove('show'),6000);}
async function reload(){state=await api('/api/state');render();}
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
    catch(error){dialog.querySelector('.error').textContent=error.message.includes('revision_conflict')?'Router 配置已变更，请取消并刷新后重新操作。':error.message;}
    finally{delete dialog.dataset.busy;controls.forEach((c,i)=>c.disabled=disabled[i]);}
  };dialog.showModal();
}
function heading(title,desc,action=""){return `<div class="heading"><div><h1>${title}</h1><p>${desc}</p></div>${action}</div>`;}
function render(){if(!state)return;$('#app').innerHTML=`<div class="shell"><aside><div class="brand">teemerchant<small>商户管理工作台</small></div>${Object.entries(names).map(([id,label])=>btn(label,'nav',id,section===id?'active':'')).join('')}<footer>商户独立身份 · 本地归档<br>经 TEE 验证后直连 Router</footer></aside><main><header><div><strong>${esc(names[section])}</strong><span class="status">${state.connected?esc(remote().merchant.name)+' · 已连接':'尚未连接'}</span></div>${btn('刷新','reload')}</header><div class="content">${state.error?`<div class="notice error">归档待重试：${esc(state.error)}</div>`:''}${view()}</div></main></div>`;bindPoolModelEditor();bindRevenueFilters();}
function view(){if(section==='revenue')return revenueView();if(section==='connection')return connection();if(!remote())return heading('连接你的 Router','验证 TEE 身份后，管理你的账号、客户和费率。')+`<section class="panel"><h2>从连接与身份开始</h2><p>先在 Router Portal 注册商户并等待审核，再在本工作台填写审核后签发的 Merchant ID 和 Merchant Secret。工作台会将凭证加密保存在本机。</p>${btn('设置连接','nav','connection','primary')}</section>`;
if(section==='overview')return heading('商户概览','你的上游账号与客户访问状态。')+`<div class="cards"><div class="card"><small>账号池</small><strong>${pools().length}</strong></div><div class="card"><small>已使用客户</small><strong>${settings().customers.length}</strong></div><div class="card"><small>价格 / 配置版本</small><strong>v${settings().revision}</strong></div></div><section class="panel"><h2>${esc(remote().merchant.name)}</h2><p>商户 ID：<code>${esc(remote().merchant.id)}</code></p><p>用户沿用平台 API key；商户标识会出现在模型和账号池名称中。</p><div class="notice">平台资源上限：并发 ${remote().merchant.max_concurrent} · RPM ${remote().merchant.rpm||'不限'} · TPM ${remote().merchant.tpm||'不限'}。实际采用平台与商户配置中较低的限额。</div></section>`;
if(section==='accounts')return accountsView();
if(section==='customers')return heading('客户授权','仅显示实际调用过本商户模型账号的用户；查询模型列表不会出现在这里。',btn('默认客户权限','customer-defaults','','primary'))+`<section class="panel"><p>平台已授权的用户首次调用时使用默认权限和限额，之后可在下方单独调整或停用。平台预授权名单不会向商户公开。</p>${table(['平台用户 ID','状态','模型 / 账号池权限','并发 / RPM / TPM','操作'],settings().customers.map(c=>`<tr><td>${esc(c.user_id)}</td><td>${c.enabled?'启用':'停用'}</td><td>${esc(c.models.join(', ')||'全部本商户模型')}</td><td>${c.max_concurrent} / ${c.rpm||'不限'} / ${c.tpm||'不限'}</td><td>${btn('编辑','customer',c.user_id)}</td></tr>`))}</section>`;
if(section==='limits')return heading('限速','设置商户总并发、每分钟请求数（RPM）和每分钟 token 数（TPM）。')+`<section class="panel"><div class="panelhead"><h2>商户总限速</h2>${btn('调整限速','limits')}</div><p>并发 ${settings().max_concurrent} · RPM ${settings().rpm||'不限'} · TPM ${settings().tpm||'不限'}</p></section>`;
if(section==='usage'||section==='refusals')return heading(names[section],'归档成功后才从 Router 确认删除；这里显示本机最近 200 条记录。',btn('立即同步','sync','','primary'))+`<section class="panel">${table(['ID','用户','模型 / 来源','详情'],records.map((r,i)=>{const e=r.event||r;return `<tr><td>${r.seq??r.id}</td><td>${esc(e.user_id||'')}</td><td>${esc(e.model||e.provider||'')}</td><td>${btn('查看','record',String(i))}</td></tr>`}))}</section>`;
}
function connection(){
  const s=state.settings,c=state.credentials,saved=!!c?.saved;
  const status=saved?(state.connected?'已保存 · 当前连接使用此凭证':'已保存 · 尚未连接 Router'):'尚未填写商户凭证';
  return heading('连接与身份','连接时会先验证 Router 的 TEE，再使用下方保存的商户凭证认证。')+
    `<section class="panel"><div class="panelhead"><h2>商户接入凭证</h2>${btn(saved?'编辑凭证':'填写接入凭证','merchant-credentials','','primary')}</div>
      <p id="merchant-credential-status">${status}</p><div class="grid credentials-grid">
      <div><label for="merchant-saved-id">Merchant ID</label><input id="merchant-saved-id" value="${esc(c?.merchant_id||'')}" readonly placeholder="尚未填写" autocomplete="off"><div class="actions"><button data-action="merchant-copy-id" ${saved?'':'disabled'}>复制 ID</button></div></div>
      <div><label for="merchant-saved-secret">Merchant Secret</label><input id="merchant-saved-secret" type="password" value="${saved?'********':''}" readonly placeholder="尚未填写" autocomplete="off"><div class="actions"><button data-action="merchant-secret" ${saved?'':'disabled'}>显示</button><button data-action="merchant-copy-secret" ${saved?'':'disabled'}>复制 Secret</button></div></div>
      </div><p class="muted">${saved?'凭证已加密保存在本机，重新打开程序时会沿用。已有账号库不能替换为其他商户身份。':'在 Portal 注册商户并通过审核后，填写平台签发的 Merchant ID 和 Merchant Secret。'}</p></section>`+
    `<section class="panel"><div class="panelhead"><h2>Router 连接</h2></div><form id="connection"><div class="grid">${field('Router HTTPS 地址','router',s.router,'url','required placeholder="https://your-router.example"')}${field('本地 TDX 信任策略路径','tdx_policy',s.tdx_policy)}${field('本地 SNP 信任策略路径','snp_policy',s.snp_policy)}</div><div class="actions"><button type="submit" class="primary">保存连接</button>${btn('验证 TEE','verify')}<button data-action="connect" ${saved?'':'disabled'}>连接商户</button></div><p class="muted">「验证 TEE」只检查 Router；「连接商户」会同时完成 TEE 验证和商户认证。</p></form></section>`;
}
async function copyMerchantValue(value){
  if(window.teemerchant)await window.teemerchant.copyText(value);
  else await navigator.clipboard.writeText(value);
}
async function toggleMerchantSecret(){
  const input=$('#merchant-saved-secret'),button=$('[data-action=merchant-secret]');
  if(!input||!state.credentials?.saved)return;
  if(input.type==='text'){input.value='********';input.type='password';button.textContent='显示';return;}
  const identity=state.credentials.merchant_id,c=await api('/api/credentials');
  if($('#merchant-saved-secret')!==input||c.merchant_id!==identity||!c.saved)return;
  input.value=c.merchant_secret;input.type='text';button.textContent='隐藏';
}
async function copyMerchantSecret(){
  const identity=state.credentials?.merchant_id,c=await api('/api/credentials');
  if(!c.saved||c.merchant_id!==identity||state.credentials?.merchant_id!==identity)throw Error('凭证已变更，请刷新后重试');
  await copyMerchantValue(c.merchant_secret);toast('Merchant Secret 已复制');
}
async function merchantCredentialsForm(){
  const c=await api('/api/credentials');
  form(c.saved?'编辑商户接入凭证':'填写商户接入凭证',field('Merchant ID','merchant_id',c.merchant_id,'text','required autocomplete="off" placeholder="m_…"')+
    field('Merchant Secret','merchant_secret',c.merchant_secret,'password','required autocomplete="off"')+
    btn('显示 Secret','merchant-edit-secret')+
    '<p>此处回填本机保存的凭证。保存后点击「连接商户」，验证 Router 并完成商户认证。</p>',async f=>{
      try{await api('/api/credentials','POST',Object.fromEntries(f));await reload();}
      finally{f.delete('merchant_secret');}
    },{submit:'保存接入凭证',success:'接入凭证已加密保存，请连接商户'});
}

function customerForm(id){
  const c=id?settings().customers.find(c=>c.user_id===id):settings().customer_defaults;
  if(!c)throw Error('请刷新客户列表后重试');
  form(id?'客户授权':'默认客户权限',(id?`<p>平台用户：<code>${esc(id)}</code></p>`:'<p>适用于首次调用及尚未单独设置权限的客户。</p>')+
    `<label><input type="checkbox" name="enabled" ${c.enabled?'checked':''}>允许调用</label>`+
    field('权限（逗号分隔；留空为本商户全部模型）','models',c.models.join(', '))+
    field('并发上限','max_concurrent',c.max_concurrent,'number','required min=1 max=4096')+
    field('RPM（0 为不限）','rpm',c.rpm,'number','required min=0')+
    field('TPM（0 为不限）','tpm',c.tpm,'number','required min=0'),async f=>{
      const d=draft(),item={enabled:f.has('enabled'),models:f.get('models').split(',').map(v=>v.trim()).filter(Boolean),max_concurrent:Number(f.get('max_concurrent')),rpm:Number(f.get('rpm')),tpm:Number(f.get('tpm'))};
      // Customer updates are patches. Unrelated edits must not freeze default
      // policies into individual overrides or erase customers discovered later.
      d.customers=id?[{user_id:id,...item}]:[];
      if(!id)d.customer_defaults=item;
      await save(d);
    });
}

async function action(a,id){
if(a==='merchant-secret')return toggleMerchantSecret();
if(a==='merchant-copy-secret')return copyMerchantSecret();
if(a==='merchant-copy-id'){await copyMerchantValue(state.credentials.merchant_id);toast('Merchant ID 已复制');return;}
if(a==='merchant-edit-secret'){const input=$('#dialog [name=merchant_secret]'),button=$('#dialog [data-action=merchant-edit-secret]');input.type=input.type==='password'?'text':'password';button.textContent=input.type==='password'?'显示 Secret':'隐藏 Secret';return;}
if(a==='close'){if($('#dialog').dataset.busy)return;$('#dialog').close();$('#dialog').innerHTML='';return;}if(a==='nav'){section=id;if(id==='revenue')await loadRevenue();if(['usage','refusals'].includes(id))records=(await api('/api/records/'+id)).data;render();return;}if(a==='reload'){if(section==='revenue')await loadRevenue();if(state.connected)await api('/api/merchant');return reload();}if(a==='verify'){const result=await api('/api/verify','POST',{});toast(result.verified?'TEE 验证通过':JSON.stringify(result));return;}if(a==='connect'){await api('/api/connect','POST',{});await reload();toast('TEE 已验证，商户已连接');return;}if(a==='merchant-credentials')return merchantCredentialsForm();if(a==='pool')return poolForm();if(a==='edit-pool')return poolForm(id);if(a==='edit-pool-models')return poolModelsForm(id);if(a==='account')return accountForm();if(a==='sync-config'){await api('/api/config/sync','POST',{});await reload();toast(remote().local_sync?.state==='pending'?'本地资料已保留，同步尚未完成':'本地配置已同步至 Router');return;}if(a==='edit-account')return accountForm(id);if(a==='import-accounts')return batchAccountForm();if(a==='account-pool'){selectedAccountPool=id;return render();}if(a==='customer')return customerForm(id);if(a==='customer-defaults')return customerForm('');if(a==='revenue-detail')return revenueDetail(id);if(a==='revenue-page'){revenueQuery.offset=id;await loadRevenue();return render();}if(a==='revenue-reset'){revenueQuery={start:'',end:'',pool:'',offset:'0'};await loadRevenue();return render();}if(a==='refresh'){await api('/api/refresh','POST',{});return reload();}if(a==='limits')return form('商户总限速',field('并发','max_concurrent',settings().max_concurrent,'number','required min=1 max=4096')+field('RPM（0 为不限）','rpm',settings().rpm,'number','required min=0')+field('TPM（0 为不限）','tpm',settings().tpm,'number','required min=0'),async f=>{const d=draft();for(const k of ['max_concurrent','rpm','tpm'])d[k]=Number(f.get(k));await save(d);});if(a==='toggle-account'){const d=draft(),a=d.pools.flatMap(p=>p.deployments).find(a=>a.id===id);a.enabled=!a.enabled;return save(d);}if(a==='delete-account')return form('删除账号',`<p>删除账号 ${esc(id)} 及其凭证，历史记录保留。</p>`,async()=>{const d=draft();for(const p of d.pools){const a=p.deployments.find(a=>a.id===id);if(a){p.deployments=p.deployments.filter(a=>a.id!==id);if(a.oauth_account)d.delete_accounts=[a.oauth_account];else d.delete_secrets=[a.api_key_ref];}}await save(d);});if(a==='sync'){await api('/api/sync','POST',{});if(section==='revenue'){await loadRevenue();render();toast('\u8d26\u5355\u5df2\u540c\u6b65');return;}records=(await api('/api/records/'+section)).data;render();toast('已同步并归档');return;}if(a==='record'){const dialog=$('#dialog');dialog.innerHTML=`<h2>记录详情</h2><pre>${esc(JSON.stringify(records[Number(id)],null,2))}</pre>${btn('关闭','close')}`;dialog.showModal();}}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;e.preventDefault();b.disabled=true;try{await action(b.dataset.action,b.dataset.id);}catch(error){toast(error.message);}finally{b.disabled=false;}});
document.addEventListener('submit',async e=>{if(e.target.id!=='connection')return;e.preventDefault();try{await api('/api/settings','PUT',Object.fromEntries(new FormData(e.target)));await reload();toast('连接设置已保存');}catch(error){toast(error.message);}});
$('#dialog').addEventListener('cancel',e=>{if($('#dialog').dataset.busy)e.preventDefault();});
$('#dialog').addEventListener('close',()=>{if(!$('#dialog').open)$('#dialog').innerHTML='';});
reload().catch(e=>toast(e.message));
