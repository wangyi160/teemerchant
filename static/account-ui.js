"use strict";
let selectedAccountPool = null;
let poolModelEditor = null;

function poolSelect(available, selected, label = '所属账号池') {
  return `<label>${label}<select name="pool" required>${available.map(p =>
    `<option value="${esc(p.name)}" ${p.name === selected ? 'selected' : ''}>${esc(p.name)} · ${esc(p.provider)}</option>`).join('')}</select></label>`;
}

function poolForm(editName = '') {
  const snapshot = structuredClone(remote());
  const existing = snapshot.pools.find(p => p.name === editName);
  if (editName && !existing) throw Error('请刷新账号池后重试');
  const rate = snapshot.settings.pool_pricing?.find(p => p.pool === editName);
  const providers = ['openai','anthropic','xai',...(snapshot.merchant.allow_test ? ['workbuddy'] : [])];
  form(existing ? '编辑账号池' : '添加账号池',
    `<div class="grid">`+
    field('账号池名称（路由标识）','name',existing?.name || '','text',`required maxlength="80" pattern="[a-zA-Z0-9_.-]+" ${existing?'readonly':''}`)+
    `<label>服务类型<select name="provider" ${existing?'disabled':''}>${providers.map(p =>
      `<option value="${p}" ${p === existing?.provider?'selected':''}>${p}</option>`).join('')}</select></label></div>`+
    field('实际倍率','multiplier',rate?String(rate.multiplier_millionths/1e6):existing?'':'1','text','required inputmode="decimal"'), async f => {
      const name = f.get('name').trim();
      await save(AccountConfig.pool(snapshot,{name,provider:existing?.provider || f.get('provider'),
        multiplier_millionths:amount(f.get('multiplier').trim())},editName || null));
      selectedAccountPool = name;
      render();
    },{submit:'保存并同步'});
}

function accountState(pool, deployment) {
  if (!deployment.enabled) return ['停用', 'muted'];
  const account = (remote().accounts?.data || []).find(a => a.name === deployment.oauth_account);
  if (account?.health?.state === 'expired') return ['已过期', 'error'];
  if (account?.health?.state === 'reauthorization_required') return ['需重新授权', 'error'];
  const entry = (remote().catalog?.data || []).find(c => c.pool === pool.name && c.deployment === deployment.id);
  if (entry?.catalog?.refreshing) return ['获取目录中', 'muted'];
  if (entry?.catalog?.last_error) return ['目录获取失败', 'error'];
  if (!entry?.catalog?.fetched_at) return ['等待模型目录', 'muted'];
  if (!Object.keys(entry.official_models || {}).length) return ['暂无模型', 'muted'];
  if (Array.isArray(pool.allowed_models) && !pool.allowed_models.some(id => Object.hasOwn(entry.official_models || {}, id)))
    return ['暂无对外模型', 'muted'];
  if (!(settings().pool_pricing || []).some(p => p.pool === pool.name)) return ['待设置倍率', 'error'];
  return ['目录已就绪', 'badge'];
}

function accountsView() {
  if (selectedAccountPool === null || selectedAccountPool && !pools().some(p => p.name === selectedAccountPool))
    selectedAccountPool = pools()[0]?.name || '';
  const chosen = pools().filter(p => !selectedAccountPool || p.name === selectedAccountPool);
  const all = pools().flatMap(p => p.deployments);
  const rows = chosen.flatMap(p => p.deployments.map(d => {
    const [status, cls] = accountState(p, d);
    return `<tr><td><strong>${esc(d.id)}</strong><small>${esc(p.name)}</small></td>
      <td>${esc(d.provider)}<small>${d.oauth_account && d.provider !== 'workbuddy' ? 'OAuth' : 'API key'}</small></td>
      <td><span class="${cls}">${status}</span></td><td>${d.max_concurrent} / ${d.rpm || '不限'}</td>
      <td class="row-actions">${btn('编辑','edit-account',d.id)} ${window.teemerchant?.oauth && d.oauth_account && ['openai','anthropic','xai'].includes(d.provider)?btn('重新授权','oauth-renew',d.id):''} ${btn(d.enabled?'停用':'启用','toggle-account',d.id)} ${btn('删除','delete-account',d.id)}</td></tr>`;
  }));
  const poolRows = pools().map(p => {
    const rate = (settings().pool_pricing || []).find(r => r.pool === p.name);
    return `<tr class="${selectedAccountPool === p.name ? 'selected-row' : ''}"><td>${btn(p.name,'account-pool',p.name)}</td>
      <td>${esc(p.provider)}</td><td>${p.deployments.length}</td><td>${rate ? usd(rate.multiplier_millionths)+'×' : '未设置'}</td>
      <td>${p.allowed_models == null ? '全部' : p.allowed_models.length+' 个'}</td>
      <td>${btn('对外模型','edit-pool-models',p.name)} ${btn('编辑账号池','edit-pool',p.name)}</td></tr>`;
  });
  const catalog = (remote().catalog?.data || []).filter(c => !selectedAccountPool || c.pool === selectedAccountPool);
  const sync = remote().local_sync;
  const pending = sync?.state === 'pending';
  const syncLabel = pending ? '本地已保存 · 等待同步' : sync?.state === 'remote_changed' ? 'Router 配置有变化，请核对本地配置后保存同步' : '本地账号配置 · 已与 Router 同步';
  return heading('账号与模型','',
    pending?btn('同步账号配置','sync-config'):'') +
    `<div class="notice">${syncLabel} · Router v${settings().revision} · ${pools().length} 个账号池 / ${all.length} 个账号。${sync?.error?`<br><span class="error">${esc(sync.error)}${sync.error.includes('revision_conflict')?'；请刷新并核对配置后重新保存。':''}</span>`:''}</div>
    ${sync?.missing_credentials?.length?`<p class="error">${sync.missing_credentials.length} 个账号缺少本地凭证</p>`:''}
    <section class="panel"><div class="panelhead"><h2>账号池</h2><div class="actions">${pools().length?btn('显示全部账号','account-pool'):''}${btn('添加账号池','pool','','primary')}</div></div>
    ${table(['账号池','服务','账号数量','倍率','对外模型','操作'],poolRows)}</section>
    <section class="panel"><div class="panelhead"><h2>${selectedAccountPool ? esc(selectedAccountPool)+' · 账号' : '全部账号'}<small class="muted">（${rows.length}）</small></h2>
    <div class="actions">${pools().length?btn('批量导入','import-accounts')+btn('添加账号','account','','primary'):''}</div></div>
    ${table(['账号别名 / 账号池','服务 / 凭证','状态','并发 / RPM','操作'],rows)}</section>
    ${poolModelCatalog(chosen, catalog)}`;
}

function poolModelsForm(poolName) {
  if (poolModelEditor && poolModelEditor.poolName !== poolName)
    throw Error('请先保存或取消 '+poolModelEditor.poolName+' 的模型编辑');
  if (!poolModelEditor) {
    const snapshot = structuredClone(remote()), pool = snapshot.pools.find(p => p.name === poolName);
    if (!pool) throw Error('账号池不存在，请刷新后重试');
    const discovered = new Set((snapshot.catalog?.data || []).filter(c => c.pool === poolName)
      .flatMap(c => Object.keys(c.official_models || {})));
    const ids = [...new Set([...discovered, ...(pool.allowed_models || [])])].sort();
    poolModelEditor = {poolName, snapshot, discovered, ids, all:pool.allowed_models == null,
      selected:new Set(pool.allowed_models ?? ids), query:'', saving:false, error:''};
  }
  selectedAccountPool = poolName;
  render();
  $('#pool-model-catalog').scrollIntoView({block:'start'});
}

function poolModelCatalog(chosen, catalog) {
  const edit = poolModelEditor?.poolName === selectedAccountPool ? poolModelEditor : null;
  let content;
  if (edit) {
    content = `<form id="pool-model-editor"><div class="catalog-editor-heading"><strong>${esc(edit.poolName)} · 编辑开放模型</strong></div>
      <div class="grid"><label>开放方式<select name="model_mode"><option value="all" ${edit.all?'selected':''}>开放全部可用模型（包含以后发现的模型）</option>
      <option value="selected" ${!edit.all?'selected':''}>仅开放勾选模型</option></select></label>
      <label>筛选模型<input id="pool-model-search" type="search" placeholder="输入模型名称" value="${esc(edit.query)}"></label></div>
      <div class="model-tools"><button type="button" id="pool-model-all">勾选筛选结果</button><button type="button" id="pool-model-none">清空勾选</button><span id="pool-model-count" aria-live="polite"></span></div>
      <div class="model-options catalog-model-options">${table(['对外开放','模型','目录状态'],edit.ids.map(id =>
        `<tr><td><input name="allowed_model" type="checkbox" value="${esc(id)}" aria-label="开放 ${esc(id)}" ${edit.selected.has(id)?'checked':''}></td><td>${esc(id)}</td><td>${edit.discovered.has(id)?'已发现':'暂未获取到目录'}</td></tr>`))}</div>
      <p class="error" role="alert">${esc(edit.error)}</p><div class="catalog-save-actions"><button type="button" id="pool-model-cancel">取消编辑</button><button type="submit" class="primary">${edit.saving?'正在保存…':'保存并同步'}</button></div></form>`;
  } else {
    content = '';
    if (poolModelEditor) content += `<div class="notice">${esc(poolModelEditor.poolName)} 的模型名单正在编辑。${btn('继续编辑','edit-pool-models',poolModelEditor.poolName)}</div>`;
    content += table(['账号池','模型','对外开放'],chosen.flatMap(p => [...new Set(catalog.filter(c => c.pool === p.name)
      .flatMap(c => Object.keys(c.official_models || {})).concat(p.allowed_models || []))].sort().map(id =>
      `<tr><td>${esc(p.name)}</td><td>${esc(id)}</td><td><input type="checkbox" disabled aria-label="${esc(p.name)} 开放 ${esc(id)}" ${p.allowed_models == null || p.allowed_models.includes(id)?'checked':''}></td></tr>`)));
  }
  return `<section class="panel" id="pool-model-catalog"><div class="panelhead"><h2>池内模型目录${selectedAccountPool?' · '+esc(selectedAccountPool):''}</h2><div class="actions">${!edit && selectedAccountPool?btn('编辑开放模型','edit-pool-models',selectedAccountPool,'primary'):''}${btn('更新目录','refresh')}</div></div>${content}</section>`;
}

function bindPoolModelEditor() {
  const form = $('#pool-model-editor'), edit = poolModelEditor;
  if (!form || !edit) return;
  const mode = form.querySelector('[name=model_mode]'), search = form.querySelector('#pool-model-search');
  const boxes = [...form.querySelectorAll('[name=allowed_model]')];
  const update = () => {
    for (const control of form.elements) control.disabled = edit.saving;
    boxes.forEach(box => {
      box.disabled = edit.saving;
      box.checked = edit.all || edit.selected.has(box.value);
      box.closest('tr').hidden = !box.value.toLowerCase().includes(edit.query.trim().toLowerCase());
    });
    form.querySelector('#pool-model-count').textContent = edit.all ? `开放全部 ${edit.ids.length} 个已发现或保留的模型，包含以后发现的模型` : `已勾选 ${edit.selected.size} / ${edit.ids.length}`;
  };
  mode.onchange = () => { edit.all = mode.value === 'all'; update(); };
  boxes.forEach(box => box.onchange = () => {
    if (edit.all) { edit.selected = new Set(edit.ids); edit.all = false; mode.value = 'selected'; }
    if (box.checked) edit.selected.add(box.value); else edit.selected.delete(box.value);
    update();
  });
  search.oninput = () => { edit.query = search.value; update(); };
  form.querySelector('#pool-model-all').onclick = () => {
    edit.all = false; mode.value = 'selected';
    boxes.filter(box => !box.closest('tr').hidden).forEach(box => edit.selected.add(box.value)); update();
  };
  form.querySelector('#pool-model-none').onclick = () => { edit.all = false; mode.value = 'selected'; edit.selected.clear(); update(); };
  form.querySelector('#pool-model-cancel').onclick = () => { if (!edit.saving) { poolModelEditor = null; render(); } };
  form.onsubmit = async e => {
    e.preventDefault();
    if (edit.saving) return;
    edit.saving = true; edit.error = ''; update();
    try {
      await save(AccountConfig.poolModels(edit.snapshot, edit.poolName, edit.all ? null : [...edit.selected]));
      poolModelEditor = null;
      toast(remote()?.local_sync?.state === 'pending' ? '模型名单已保存到本地，等待同步 Router' : '模型名单已保存并同步至 Router');
    } catch (error) {
      edit.error = error.message.includes('revision_conflict') ? '配置已变更。请取消编辑、刷新后重试；本次模型名单没有覆盖新配置。' : error.message;
    } finally { edit.saving = false; render(); }
  };
  update();
}

async function accountForm(editId = '') {
  const identity = state.enrollment.id, router = state.settings.router;
  const snapshot = structuredClone(remote());
  if (!snapshot.pools.length) throw Error('请先添加账号池');
  const originalPool = snapshot.pools.find(p => p.deployments.some(d => d.id === editId));
  const account = originalPool?.deployments.find(d => d.id === editId);
  if (editId && !account) throw Error('请刷新账号列表后重试');
  const local = account ? await api('/api/accounts/'+encodeURIComponent(editId)) : null;
  if (state.enrollment.id !== identity || state.settings.router !== router) throw Error('商户连接已变化，请重新打开');
  if (local && local.local_revision !== snapshot.local_revision) throw Error('本地配置已变更，请刷新后再编辑');
  const available = snapshot.pools.filter(p => !account || p.provider === account.provider);
  const kind = account?.oauth_account && account.provider !== 'workbuddy' ? 'oauth' : 'api';
  const originalCredential = local?.credential_text ? (kind === 'api' ? JSON.stringify({provider:account.provider,credential:{api_key:local.credential_text}},null,2) : local.credential_text) : '';
  const originalProxy = local?.proxy || '';
  const canAuthorize = !!window.teemerchant?.oauth && (!account || kind === 'oauth');
  let oauthEditor;
  form(account ? '编辑账号' : '添加上游账号',
    `<div class="grid">`+
    field('账号 ID（别名）','id',account?.id || '','text',`required maxlength="128" pattern="[a-zA-Z0-9_.-]+" ${account?'readonly':''}`)+
    poolSelect(available,originalPool?.name || selectedAccountPool)+
    `</div><div class="panelhead"><label for="account-credential">凭证 JSON</label>${canAuthorize?'<div id="account-oauth-actions" class="actions"><label><input type="checkbox" name="save_refresh">保留 refresh token</label><button type="button" id="account-authorize">OAuth 授权</button></div>':''}</div>
    <textarea id="account-credential" name="credential" rows="10" ${account?'':'required'} autocomplete="off" spellcheck="false">${esc(originalCredential)}</textarea>
    ${canAuthorize?'<div id="oauth-progress" aria-live="polite" hidden></div>':''}`+
    field('账号网络代理','proxy',originalProxy,'text','autocomplete="off"')+
    `<div class="grid">`+field('并发上限','max_concurrent',account?.max_concurrent ?? 16,'number','required min="1" max="4096"')+
    field('RPM（0 为不限）','rpm',account?.rpm ?? 0,'number','required min="0" max="10000000"')+`</div>
    <label><input name="enabled" type="checkbox" ${!account || account.enabled?'checked':''}>启用账号</label>`, async f => {
      oauthEditor?.ensureReady();
      if (state.enrollment.id !== identity || state.settings.router !== router) throw Error('商户连接已变化，请重新打开');
      const entry = {id:f.get('id'),pool:f.get('pool'),provider:available.find(p=>p.name===f.get('pool'))?.provider,
        kind:account ? kind : undefined,max_concurrent:Number(f.get('max_concurrent')),
        rpm:Number(f.get('rpm')),enabled:f.has('enabled')};
      const raw = f.get('credential').trim();
      if (raw && raw !== originalCredential.trim()) {
        const value = AccountConfig.accountInput(raw,entry.provider);
        const original = originalCredential ? AccountConfig.accountInput(originalCredential,entry.provider) : null;
        entry.kind = value.kind;
        if (JSON.stringify(value) !== JSON.stringify(original)) {
          if (value.kind === 'api') entry.api_key = value.api_key;
          else entry.credential = value.credential;
          if (value.proxy) entry.proxy = value.proxy;
        }
      }
      if (f.get('proxy').trim() && f.get('proxy').trim() !== originalProxy) entry.proxy = f.get('proxy').trim();
      await save(AccountConfig.build(snapshot,[entry],{},editId || null).config);
    },{submit:'保存并同步'});
  if(canAuthorize){
    const dialog=$('#dialog'),setup=dialog.querySelector('form');setup.id='oauth-setup';
    let previousRefresh=false;
    if(originalCredential){try{const c=AccountConfig.oauthInput(originalCredential,account.provider).credential;previousRefresh=!!(c.refresh_token||c.claudeAiOauth?.refreshToken);}catch{}}
    oauthEditor=attachOAuthForm({dialog,setup,progress:$('#oauth-progress'),identity,available,account,editId,previousRefresh});
  }
}

function batchAccountForm() {
  const snapshot = structuredClone(remote());
  if (!snapshot.pools.length) throw Error('请先添加账号池');
  let preview = null, fileGeneration = 0;
  form('批量导入账号',`${poolSelect(snapshot.pools,selectedAccountPool,'默认账号池')}
    <label>选择 JSON 文件（支持多选）<input id="import-files" type="file" accept=".json,application/json" multiple></label>
    <div class="panelhead"><span class="muted">账号 JSON</span><button id="import-example" type="button">下载示例</button></div>
    <label><textarea name="entries" rows="12" required spellcheck="false" autocomplete="off" placeholder='[{"id":"account-001","proxy":{"scheme":"socks5h","host":"proxy-1.example","port":1080,"username":"user1","password":"…"},"api_key":"…"}]'></textarea></label>
    <button id="import-preview" type="button">校验并预览</button><div id="import-review" aria-live="polite"></div>`,async () => {
      if (!preview) throw Error('请先校验并预览');
      await save(preview.config);
      preview = null;
    },{submit:'确认导入'});
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
        {pool:f.get('pool')});
      $('#import-review').innerHTML=`<p><strong>${result.rows.length} 个账号校验通过，尚未提交。</strong>确认后一次提交；账号凭证和代理密码不在预览中显示。</p>`+
        table(['账号别名','账号池 / 倍率','服务 / 凭证','账号代理（认证已隐藏）','并发 / RPM'],result.rows.map(r=>
          `<tr><td>${esc(r.id)}</td><td>${esc(r.pool)}<small>${r.multiplier_millionths===undefined?'待设置倍率':usd(r.multiplier_millionths)+'×'}</small></td><td>${esc(r.provider)} / ${r.kind==='api'?'API key':'OAuth'}</td><td>${esc(r.proxy)}</td><td>${r.max_concurrent} / ${r.rpm||'不限'}</td></tr>`));
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
    const target = snapshot.pools.find(p=>p.name===formEl.elements.pool.value);
    const data=[1,2].map(n=>({id:`account-00${n}`,pool:target.name,
      proxy:target.provider==='workbuddy'?'direct://':{scheme:'socks5h',host:`proxy-${n}.example`,port:1080,
        username:`REPLACE_PROXY_USER_${n}`,password:`REPLACE_PROXY_PASSWORD_${n}`},
      api_key:`REPLACE_API_KEY_${n}`}));
    if (target.provider === 'xai') {
      delete data[0].api_key;
      data[0].credential = {'https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828':{
        key:'REPLACE_ACCESS_TOKEN',refresh_token:'REPLACE_REFRESH_TOKEN',
        expires_at:'2030-01-01T00:00:00.000Z',token_type:'Bearer'}};
    }
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='merchant-accounts.example.json';link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
}
