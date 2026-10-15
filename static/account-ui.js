"use strict";
let selectedAccountPool = null;

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
    `<p>${existing ? '账号池标识和服务类型固定；倍率适用于池内全部账号和模型。' : '先创建账号池，再添加或批量导入账号。同一池内的账号使用相同服务类型和倍率。'}</p><div class="grid">`+
    field('账号池名称（路由标识）','name',existing?.name || '','text',`required maxlength="80" pattern="[a-zA-Z0-9_.-]+" ${existing?'readonly':''}`)+
    `<label>服务类型<select name="provider" ${existing?'disabled':''}>${providers.map(p =>
      `<option value="${p}" ${p === existing?.provider?'selected':''}>${p}</option>`).join('')}</select></label></div>`+
    field('价格倍率（Workbuddy 使用测试基准）','multiplier',rate?String(rate.multiplier_millionths/1e6):'1','text','required inputmode="decimal"')+
    '<p>例如 0.2 表示官方价格的 20%，1 表示原价。空池不会提供模型服务。</p>', async f => {
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
      <td class="row-actions">${btn('编辑','edit-account',d.id)} ${btn(d.enabled?'停用':'启用','toggle-account',d.id)} ${btn('删除','delete-account',d.id)}</td></tr>`;
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
  return heading('账号与模型','先管理账号池，再选择池管理其中的账号。资料保存在本机，连接后同步到 Router。',
    pending?btn('同步账号配置','sync-config'):'') +
    `<div class="notice">${syncLabel} · Router v${settings().revision} · ${pools().length} 个账号池 / ${all.length} 个账号。${sync?.error?`<br><span class="error">${esc(sync.error)}${sync.error.includes('revision_conflict')?'；请刷新并核对配置后重新保存。':''}</span>`:''}</div>
    ${sync?.missing_credentials?.length?`<p class="error">${sync.missing_credentials.length} 个旧版账号尚缺本地凭证，可在编辑中补录；Router 中的原凭证仍保留。</p>`:''}
    <section class="panel"><div class="panelhead"><h2>账号池</h2><div class="actions">${pools().length?btn('显示全部账号','account-pool'):''}${btn('添加账号池','pool','','primary')}</div></div>
    ${pools().length?'':'<p>尚无账号池。先添加账号池并设置服务类型和倍率，再向池内添加账号。</p>'}
    ${table(['账号池','服务','账号数量','倍率','对外模型','操作'],poolRows)}</section>
    <section class="panel"><div class="panelhead"><h2>${selectedAccountPool ? esc(selectedAccountPool)+' · 账号' : '全部账号'}<small class="muted">（${rows.length}）</small></h2>
    <div class="actions">${pools().length?btn('批量导入','import-accounts')+btn('添加账号','account','','primary'):''}</div></div>
    ${selectedAccountPool && !rows.length?'<p>此账号池还没有账号，可以逐个添加或批量导入。</p>':''}
    ${table(['账号别名 / 账号池','服务 / 凭证','状态','并发 / RPM','操作'],rows)}</section>
    <section class="panel"><div class="panelhead"><h2>池内模型目录</h2>${btn('更新目录','refresh')}</div>
    <p>对外模型名单由账号池统一设置，池内所有账号共用；实际可调用模型还须通过 Router 的可用性、权限和价格校验。</p>
    ${table(['账号池','模型','对外策略'],chosen.flatMap(p => [...new Set(catalog.filter(c => c.pool === p.name)
      .flatMap(c => Object.keys(c.official_models || {})).concat(p.allowed_models || []))].sort().map(id =>
      `<tr><td>${esc(p.name)}</td><td>${esc(id)}</td><td>${p.allowed_models == null || p.allowed_models.includes(id) ? '开放' : '不开放'}</td></tr>`)))}</section>`;
}

function poolModelsForm(poolName) {
  const snapshot = structuredClone(remote()), pool = snapshot.pools.find(p => p.name === poolName);
  if (!pool) throw Error('账号池不存在，请刷新后重试');
  const discovered = new Set((snapshot.catalog?.data || []).filter(c => c.pool === poolName)
    .flatMap(c => Object.keys(c.official_models || {})));
  const ids = [...new Set([...discovered, ...(pool.allowed_models || [])])].sort();
  form(poolName+' · 对外模型', `<p>此名单对池内全部账号生效。未开放的模型不会展示给用户，也不能通过指定池或自动路由调用。</p>
    <label>开放方式<select name="model_mode"><option value="all" ${pool.allowed_models == null ? 'selected' : ''}>开放全部可用模型（包含以后发现的模型）</option>
    <option value="selected" ${pool.allowed_models != null ? 'selected' : ''}>仅开放勾选模型</option></select></label>
    <p>仅开放勾选模型时，不勾选任何模型表示暂停该池的全部模型服务。暂未获取到目录的已选项会保留，但不会绕过 Router 的实际能力校验。</p>
    <div id="pool-model-picker"><label>筛选模型<input id="pool-model-search" type="search" placeholder="输入模型名称"></label>
    <div class="model-tools"><button type="button" id="pool-model-all">勾选筛选结果</button><button type="button" id="pool-model-none">清空勾选</button><span id="pool-model-count"></span></div>
    <div class="model-options">${ids.length ? ids.map(id => `<label data-model="${esc(id)}"><input name="allowed_model" type="checkbox" value="${esc(id)}" ${pool.allowed_models == null || pool.allowed_models.includes(id) ? 'checked' : ''}>${esc(id)}${discovered.has(id) ? '' : '<small class="muted"> · 暂未获取到目录</small>'}</label>`).join('') : '<p>尚无模型目录。请先添加账号并更新目录，再选择对外模型。</p>'}</div></div>`,
    async f => save(AccountConfig.poolModels(snapshot, poolName, f.get('model_mode') === 'all' ? null : f.getAll('allowed_model'))),
    {submit:'保存并同步'});
  const dialog = $('#dialog'), mode = dialog.querySelector('[name=model_mode]');
  const boxes = [...dialog.querySelectorAll('[name=allowed_model]')];
  const update = () => {
    const all = mode.value === 'all';
    dialog.querySelector('#pool-model-picker').hidden = all;
    boxes.forEach(box => box.disabled = all);
    dialog.querySelector('#pool-model-count').textContent = `已勾选 ${boxes.filter(box => box.checked).length} / ${ids.length}`;
  };
  mode.onchange = update;
  boxes.forEach(box => box.onchange = update);
  dialog.querySelector('#pool-model-search').oninput = e => boxes.forEach(box => {
    box.parentElement.hidden = !box.value.toLowerCase().includes(e.target.value.trim().toLowerCase());
  });
  dialog.querySelector('#pool-model-all').onclick = () => { boxes.filter(box => !box.parentElement.hidden).forEach(box => box.checked = true); update(); };
  dialog.querySelector('#pool-model-none').onclick = () => { boxes.forEach(box => box.checked = false); update(); };
  update();
}

async function accountForm(editId = '') {
  const snapshot = structuredClone(remote());
  if (!snapshot.pools.length) throw Error('请先添加账号池');
  const originalPool = snapshot.pools.find(p => p.deployments.some(d => d.id === editId));
  const account = originalPool?.deployments.find(d => d.id === editId);
  if (editId && !account) throw Error('请刷新账号列表后重试');
  const local = account ? await api('/api/accounts/'+encodeURIComponent(editId)) : null;
  if (local && local.local_revision !== snapshot.local_revision) throw Error('本地配置已变更，请刷新后再编辑');
  const originalCredential = local?.credential_text || '', originalProxy = local?.proxy || '';
  const available = snapshot.pools.filter(p => !account || p.provider === account.provider);
  const kind = account?.oauth_account && account.provider !== 'workbuddy' ? 'oauth' : 'api';
  form(account ? '编辑账号' : '添加上游账号',
    `<p>${account ? (local.credential_present?'已读取本机保存的账号资料。未修改的凭证不会重复发送到 Router。':'旧版账号尚无本地凭证，可在此补录；留空会保留 Router 中的原凭证。') : '账号 ID 是对外展示的别名，勿填写真实邮箱。'} 保存时先写入本地账号库。</p><div class="grid">`+
    field('账号 ID（别名）','id',account?.id || '','text',`required maxlength="128" pattern="[a-zA-Z0-9_.-]+" ${account?'readonly':''}`)+
    poolSelect(available,originalPool?.name || selectedAccountPool)+
    `<label>凭证类型<select name="kind" ${account?'disabled':''}><option value="api" ${kind==='api'?'selected':''}>API key</option><option value="oauth" ${kind==='oauth'?'selected':''}>OAuth 账号 JSON</option></select></label></div>
    <p>服务类型和倍率继承所属账号池。需要新池时，请先在账号池列表中添加。</p>
    <label>凭证（本地保存）<textarea name="credential" ${account?'':'required'} autocomplete="off" spellcheck="false" placeholder="API key，或包含 provider 和 credential 的 OAuth JSON；xai 支持直接粘贴 Grok 原生凭证 JSON">${esc(originalCredential)}</textarea></label>
    <p>OAuth 可只提供 access_token，省略 refresh_token 时不自动续期，到期后显示“已过期”，需手动更新凭证。JWT 会读取自带的到期时间；不含到期时间的令牌可填写 expires_at（Unix 秒）。</p>
    <p>xai 池选择 OAuth 后，可直接粘贴 Grok 原生凭证 JSON，程序会自动转换 key 和到期时间；账号别名、网络代理仍在本窗口填写。</p>`+
    field('账号网络代理（本地保存）','proxy',originalProxy,'text','autocomplete="off" placeholder="socks5h://代理服务器:1080；Workbuddy 可填 direct://"')+
    `<div class="grid">`+field('并发上限','max_concurrent',account?.max_concurrent ?? 16,'number','required min="1" max="4096"')+
    field('RPM（0 为不限）','rpm',account?.rpm ?? 0,'number','required min="0" max="10000000"')+`</div>
    <label><input name="enabled" type="checkbox" ${!account || account.enabled?'checked':''}>启用账号</label>`, async f => {
      const entry = {id:f.get('id'),pool:f.get('pool'),provider:available.find(p=>p.name===f.get('pool'))?.provider,
        kind:account ? kind : f.get('kind'),max_concurrent:Number(f.get('max_concurrent')),
        rpm:Number(f.get('rpm')),enabled:f.has('enabled')};
      const raw = f.get('credential').trim();
      if (raw && raw !== originalCredential.trim()) {
        if (entry.kind === 'api') entry.api_key = raw;
        else {
          const value = AccountConfig.oauthInput(raw,entry.provider);
          const original = originalCredential ? AccountConfig.oauthInput(originalCredential,entry.provider).credential : null;
          if (JSON.stringify(value.credential) !== JSON.stringify(original)) entry.credential = value.credential;
          if (value.proxy) entry.proxy = value.proxy;
        }
      }
      if (f.get('proxy').trim() && f.get('proxy').trim() !== originalProxy) entry.proxy = f.get('proxy').trim();
      await save(AccountConfig.build(snapshot,[entry],{},editId || null).config);
    },{submit:'保存并同步'});
}

function batchAccountForm() {
  const snapshot = structuredClone(remote());
  if (!snapshot.pools.length) throw Error('请先添加账号池');
  let preview = null, fileGeneration = 0;
  form('批量导入账号',`<p>每个账号对象必须包含 id、proxy，以及 credential（OAuth）或 api_key。代理与该账号单独绑定，不使用默认代理。服务类型和倍率继承账号池；单项可指定另一个已有池。</p>
    ${poolSelect(snapshot.pools,selectedAccountPool,'默认账号池')}
    <p>proxy 推荐填写为对象：scheme、host、port，以及可选的 username、password；也兼容代理 URL。仅 Workbuddy 测试账号允许填写 "direct://"。已有账号请使用“编辑”。</p>
    <p>Grok 原生凭证可原样放入每条账号的 credential 中；仍须为每条账号填写 id 和 proxy。可省略 refresh_token，仅使用 access_token，到期后手动更新。</p>
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
