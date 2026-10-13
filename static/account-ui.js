"use strict";
let selectedAccountPool = '';

function accountState(pool, deployment) {
  if (!deployment.enabled) return ['停用', 'muted'];
  const account = (remote().accounts?.data || []).find(a => a.name === deployment.oauth_account);
  if (account?.health?.state === 'reauthorization_required') return ['需重新授权', 'error'];
  const entry = (remote().catalog?.data || []).find(c => c.pool === pool.name && c.deployment === deployment.id);
  if (entry?.catalog?.refreshing) return ['获取目录中', 'muted'];
  if (entry?.catalog?.last_error) return ['目录获取失败', 'error'];
  if (!entry?.catalog?.fetched_at) return ['等待模型目录', 'muted'];
  if (!Object.keys(entry.official_models || {}).length) return ['暂无模型', 'muted'];
  if (!(settings().pool_pricing || []).some(p => p.pool === pool.name)) return ['待设置倍率', 'error'];
  return ['目录已就绪', 'badge'];
}

function accountsView() {
  if (selectedAccountPool && !pools().some(p => p.name === selectedAccountPool)) selectedAccountPool = '';
  const chosen = pools().filter(p => !selectedAccountPool || p.name === selectedAccountPool);
  const all = pools().flatMap(p => p.deployments);
  const rows = chosen.flatMap(p => p.deployments.map(d => {
    const [status, cls] = accountState(p, d);
    return `<tr><td><strong>${esc(d.id)}</strong><small>${esc(p.name)}</small></td>
      <td>${esc(d.provider)}<small>${d.oauth_account && d.provider !== 'workbuddy' ? 'OAuth' : 'API key'}</small></td>
      <td><span class="${cls}">${status}</span></td><td>${d.max_concurrent} / ${d.rpm || '不限'}</td>
      <td class="row-actions">${btn('编辑','edit-account',d.id)} ${btn(d.enabled?'停用':'启用','toggle-account',d.id)} ${btn('删除','delete-account',d.id)}</td></tr>`;
  }));
  const poolRows = pools().map(p => {
    const rate = (settings().pool_pricing || []).find(r => r.pool === p.name);
    return `<tr class="${selectedAccountPool === p.name ? 'selected-row' : ''}"><td>${btn(p.name,'account-pool',p.name)}</td>
      <td>${esc(p.provider)}</td><td>${p.deployments.length}</td><td>${rate ? usd(rate.multiplier_millionths)+'×' : '未设置'}</td>
      <td>${btn('修改倍率','price',p.name)}</td></tr>`;
  });
  const catalog = (remote().catalog?.data || []).filter(c => !selectedAccountPool || c.pool === selectedAccountPool);
  return heading('账号与模型','保存后立即提交到 Router；账号凭证和代理不会回显。',
    `<div class="actions">${btn('批量导入','import-accounts')}${btn('添加账号','account','','primary')}</div>`) +
    `<div class="notice">已提交至 Router · 配置 v${settings().revision} · ${pools().length} 个账号池 / ${all.length} 个账号。模型目录就绪后，调用仍受账号额度、倍率和权限限制。</div>
    <section class="panel"><div class="panelhead"><h2>账号池</h2>${btn('显示全部账号','account-pool')}</div>
    ${table(['账号池','服务','账号数量','倍率','操作'],poolRows)}</section>
    <section class="panel"><div class="panelhead"><h2>${selectedAccountPool ? esc(selectedAccountPool)+' · 账号' : '全部账号'}</h2><span class="muted">${rows.length} 个账号</span></div>
    ${table(['账号别名 / 账号池','服务 / 凭证','状态','并发 / RPM','操作'],rows)}</section>
    <section class="panel"><div class="panelhead"><h2>官方模型目录</h2>${btn('更新目录','refresh')}</div>
    ${table(['账号池','账号','模型'],catalog.flatMap(c => Object.keys(c.official_models || {}).map(id =>
      `<tr><td>${esc(c.pool)}</td><td>${esc(c.deployment)}</td><td>${esc(id)}</td></tr>`)))}</section>`;
}

function accountForm(editId = '') {
  const snapshot = structuredClone(remote());
  const originalPool = snapshot.pools.find(p => p.deployments.some(d => d.id === editId));
  const account = originalPool?.deployments.find(d => d.id === editId);
  if (editId && !account) throw Error('请刷新账号列表后重试');
  const providers = ['openai','anthropic','xai',...(snapshot.merchant.allow_test ? ['workbuddy'] : [])];
  const kind = account?.oauth_account && account.provider !== 'workbuddy' ? 'oauth' : 'api';
  form(account ? '编辑账号' : '添加上游账号',
    `<p>${account ? '凭证和代理留空时保留原值。账号 ID、服务和凭证类型固定。' : '账号 ID 是对外展示的别名，勿填写真实邮箱。'}</p><div class="grid">`+
    field('账号 ID（别名）','id',account?.id || '','text',`required maxlength="128" pattern="[a-zA-Z0-9_.-]+" ${account?'readonly':''}`)+
    field('账号池名称','pool',originalPool?.name || selectedAccountPool,'text','required maxlength="80" pattern="[a-zA-Z0-9_.-]+" list="pool-options"')+
    `<label>服务<select name="provider" ${account?'disabled':''}>${providers.map(p => `<option ${p === (account?.provider || snapshot.pools.find(p=>p.name===selectedAccountPool)?.provider) ? 'selected' : ''}>${p}</option>`).join('')}</select></label>
    <label>凭证类型<select name="kind" ${account?'disabled':''}><option value="api" ${kind==='api'?'selected':''}>API key</option><option value="oauth" ${kind==='oauth'?'selected':''}>OAuth 账号 JSON</option></select></label></div>
    <datalist id="pool-options">${snapshot.pools.map(p => `<option value="${esc(p.name)}"></option>`).join('')}</datalist>
    <label>${account?'更换凭证（留空保留）':'凭证'}<textarea name="credential" ${account?'':'required'} autocomplete="off" spellcheck="false" placeholder="API key，或包含 provider 和 credential 的 OAuth JSON"></textarea></label>`+
    field(account?'更换网络代理（留空保留）':'账号网络代理（也可在 OAuth JSON 中填写）','proxy','','text','autocomplete="off" placeholder="socks5h://代理服务器:1080；Workbuddy 可填 direct://"')+
    `<div class="grid">`+field('并发上限','max_concurrent',account?.max_concurrent ?? 16,'number','required min="1" max="4096"')+
    field('RPM（0 为不限）','rpm',account?.rpm ?? 0,'number','required min="0" max="10000000"')+
    field('新账号池倍率（已有池保持原倍率）','multiplier','1','text','required inputmode="decimal"')+`</div>
    <label><input name="enabled" type="checkbox" ${!account || account.enabled?'checked':''}>启用账号</label>`, async f => {
      const entry = {id:f.get('id'),pool:f.get('pool'),provider:account?.provider || f.get('provider'),
        kind:account ? kind : f.get('kind'),max_concurrent:Number(f.get('max_concurrent')),
        rpm:Number(f.get('rpm')),enabled:f.has('enabled')};
      const raw = f.get('credential').trim();
      if (raw) {
        if (entry.kind === 'api') entry.api_key = raw;
        else {
          const values = AccountConfig.parse(raw), value = values[0];
          if (values.length !== 1 || value.provider !== entry.provider || !value.credential ||
              Object.keys(value).some(k => !['provider','credential','proxy','enabled'].includes(k)))
            throw Error('OAuth JSON 须包含匹配的 provider 和 credential；多个账号请使用批量导入');
          entry.credential = value.credential;
          if (value.proxy) entry.proxy = value.proxy;
        }
      }
      if (f.get('proxy').trim()) entry.proxy = f.get('proxy').trim();
      await save(AccountConfig.build(snapshot,[entry],{multiplier_millionths:amount(f.get('multiplier').trim())},editId || null).config);
    },{submit:'保存并提交',success:'账号已提交至 Router；可查看模型目录状态'});
}

function batchAccountForm() {
  const snapshot = structuredClone(remote());
  let preview = null, fileGeneration = 0;
  const providers = ['openai','anthropic','xai',...(snapshot.merchant.allow_test ? ['workbuddy'] : [])];
  form('批量导入账号',`<p>选择 JSON 文件或粘贴账号数组。每项包含独立的 id 和 credential（OAuth）或 api_key；单项配置优先于下方默认值。已有账号请使用“编辑”。</p>
    <div class="grid">${field('默认账号池','pool',selectedAccountPool,'text','maxlength="80" pattern="[a-zA-Z0-9_.-]+"')}
    <label>默认服务<select name="provider">${providers.map(p=>`<option ${p===snapshot.pools.find(p=>p.name===selectedAccountPool)?.provider?'selected':''}>${p}</option>`).join('')}</select></label>
    ${field('默认网络代理（可在各账号中单独填写）','proxy','','text','autocomplete="off"')}
    ${field('新账号池倍率（已有池保持原倍率）','multiplier','1','text','required inputmode="decimal"')}</div>
    <label>选择 JSON 文件（支持多选）<input id="import-files" type="file" accept=".json,application/json" multiple></label>
    <div class="panelhead"><span class="muted">账号 JSON</span><button id="import-example" type="button">下载示例</button></div>
    <label><textarea name="entries" rows="9" required spellcheck="false" autocomplete="off" placeholder='[{"id":"openai-001","credential":{"access_token":"…","refresh_token":"…"}}]'></textarea></label>
    <button id="import-preview" type="button">校验并预览</button><div id="import-review" aria-live="polite"></div>`,async () => {
      if (!preview) throw Error('请先校验并预览');
      await save(preview.config);
      preview = null;
    },{submit:'确认导入',success:'批量账号已提交至 Router；可查看模型目录状态'});
  const dialog = $('#dialog'), formEl = dialog.querySelector('form'), submit = formEl.querySelector('[type=submit]');
  dialog.classList.add('wide');
  submit.disabled = true;
  const clear = () => {preview=null;submit.disabled=true;$('#import-review').innerHTML='';};
  formEl.addEventListener('input',clear);
  formEl.addEventListener('change',clear);
  $('#import-preview').onclick = () => {
    clear();
    dialog.querySelector('.error').textContent='';
    try {
      const f = new FormData(formEl);
      const result = AccountConfig.build(snapshot,AccountConfig.parse(f.get('entries')),
        {pool:f.get('pool').trim(),provider:f.get('provider'),proxy:f.get('proxy').trim(),multiplier_millionths:amount(f.get('multiplier').trim())});
      $('#import-review').innerHTML=`<p><strong>${result.rows.length} 个账号校验通过，尚未提交。</strong>确认后一次提交；账号凭证和代理密码不在预览中显示。</p>`+
        table(['账号别名','账号池 / 倍率','服务 / 凭证','网络出口','并发 / RPM'],result.rows.map(r=>
          `<tr><td>${esc(r.id)}</td><td>${esc(r.pool)}<small>${r.multiplier_millionths===undefined?'待设置倍率':usd(r.multiplier_millionths)+'×'}</small></td><td>${esc(r.provider)} / ${r.kind==='api'?'API key':'OAuth'}</td><td>${r.proxy}</td><td>${r.max_concurrent} / ${r.rpm||'不限'}</td></tr>`));
      preview=result;
      submit.disabled=false;
    } catch(error) {dialog.querySelector('.error').textContent=error.message;}
  };
  $('#import-files').onchange = async e => {
    clear();
    const generation=++fileGeneration;
    const files=Array.from(e.target.files), entries=[];
    const textarea=formEl.elements.entries;
    textarea.value='';
    try {
      if(files.reduce((n,f)=>n+f.size,0)>12*1024*1024) throw Error('导入文件总大小不能超过 12 MB');
      for(const file of files) entries.push(...AccountConfig.parse(await file.text()));
      if (!dialog.open || dialog.querySelector('form') !== formEl || generation !== fileGeneration) return;
      clear();
      textarea.value=JSON.stringify(entries,null,2);
      dialog.querySelector('.error').textContent='';
    } catch(error) {if (dialog.open && dialog.querySelector('form') === formEl && generation === fileGeneration) dialog.querySelector('.error').textContent=error.message;}
  };
  formEl.elements.entries.addEventListener('input',()=>fileGeneration++);
  $('#import-example').onclick = () => {
    const data=[{id:'openai-001',pool:'openai',provider:'openai',proxy:'socks5h://proxy.example:1080',
      credential:{access_token:'REPLACE_ACCESS_TOKEN',refresh_token:'REPLACE_REFRESH_TOKEN',id_token:'REPLACE_ID_TOKEN',account_id:'REPLACE_OFFICIAL_ACCOUNT_ID'}},
      {id:'openai-002',pool:'openai',provider:'openai',proxy:'socks5h://proxy.example:1080',api_key:'REPLACE_API_KEY'}];
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='merchant-accounts.example.json';link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
}
