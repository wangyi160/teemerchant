/* Shared by the browser bundle and Node tests. No network or persistent storage. */
"use strict";
const AccountConfig = (() => {
  const clone = value => structuredClone(value);
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const name = (value, label, max = 128) => {
    if (typeof value !== 'string' || !/^[a-zA-Z0-9_.-]+$/.test(value) || value.length > max)
      throw Error(`${label}须为 1–${max} 位字母、数字、点、下划线或连字符`);
    return value;
  };
  function integer(value, label, min, max) {
    if (!Number.isSafeInteger(value) || value < min || value > max) throw Error(`${label}须为 ${min}–${max} 的整数`);
    return value;
  }
  function proxy(value, provider) {
    if (provider === 'workbuddy' && value === 'direct://') return value;
    try {
      if (object(value)) {
        const fields = ['scheme','host','port','username','password'];
        if (Object.keys(value).some(k => !fields.includes(k)) ||
            !['http','https','socks5h'].includes(value.scheme) ||
            typeof value.host !== 'string' || !value.host || /[\s/@?#\\]/.test(value.host) ||
            !Number.isInteger(value.port) || value.port < 1 || value.port > 65535 ||
            [value.username,value.password].some(v => v !== undefined && typeof v !== 'string') ||
            value.password && !value.username) throw Error();
        const url = new URL(`${value.scheme}://${value.host}:${value.port}`);
        // Encode each field separately so @, :, /, %, etc. remain credential data.
        url.username = encodeURIComponent(value.username || '');
        url.password = encodeURIComponent(value.password || '');
        value = url.href;
      }
      if (typeof value !== 'string' || value.trim() !== value || /[\x00-\x1f\x7f]/.test(value)) throw Error();
      const url = new URL(value);
      if (!['http:', 'https:', 'socks5h:'].includes(url.protocol) || !url.hostname || url.hash || url.search || !['','/'].includes(url.pathname) || url.port === '0')
        throw Error();
      return value;
    } catch { throw Error('proxy 格式无效：请填写 scheme（http/https/socks5h）、host、port（1–65535）及可选 username/password，或有效代理 URL；仅 Workbuddy 可用 direct://'); }
  }
  function proxyLabel(value) {
    if (value === 'direct://') return value;
    const url = new URL(value);
    return `${url.protocol}//${url.username || url.password ? '***:***@' : ''}${url.host}`;
  }
  function parse(text) {
    if (typeof text !== 'string' || new TextEncoder().encode(text).length > 12*1024*1024)
      throw Error('导入 JSON 不能超过 12 MB');
    let value;
    try { value = JSON.parse(text.replace(/^\uFEFF/, '')); }
    catch { throw Error('JSON 格式无效，请检查双引号、逗号及括号'); }
    const entries = Array.isArray(value) ? value : [value];
    if (!entries.length || entries.length > 4096 || entries.some(row => !object(row)))
      throw Error('请提供 1–4096 个账号对象');
    return entries;
  }
  function draft(remote) {
    if (!remote) throw Error('请先验证并连接 Router');
    const s = remote.settings;
    return {expected_revision:s.revision, _local_revision:remote.local_revision, pools:clone(remote.pools), customers:[],
      customer_defaults:clone(s.customer_defaults), pool_pricing:clone(s.pool_pricing || []),
      max_concurrent:s.max_concurrent, rpm:s.rpm, tpm:s.tpm};
  }
  function pool(remote, entry, editName = null) {
    const config = draft(remote);
    const poolName = name(entry.name, '账号池名称', 80);
    const existing = config.pools.find(p => p.name === (editName || poolName));
    if (editName && (!existing || poolName !== editName)) throw Error('账号池标识不可变更，请刷新后重试');
    if (!editName && existing) throw Error('账号池名称已存在');
    if (!['openai','anthropic','xai','workbuddy'].includes(entry.provider) ||
        (entry.provider === 'workbuddy' && !remote.merchant.allow_test))
      throw Error('该服务未获授权或不受支持');
    if (existing && existing.provider !== entry.provider) throw Error('账号池服务类型不可变更');
    const multiplier = integer(entry.multiplier_millionths, '账号池倍率', 0, 1e15);
    if (!existing) {
      if (config.pools.length >= 1024) throw Error('账号池数量已达上限');
      config.pools.push({name:poolName, provider:entry.provider, deployments:[]});
    }
    config.pool_pricing = config.pool_pricing.filter(p => p.pool !== poolName);
    config.pool_pricing.push({pool:poolName, multiplier_millionths:multiplier});
    return config;
  }
  function build(remote, entries, defaults = {}, editId = null) {
    if (!Array.isArray(entries) || !entries.length || entries.length > 4096 || (editId && entries.length !== 1))
      throw Error('账号数量无效');
    const config = draft(remote), rows = [], seen = new Set();
    const deployments = remote.pools.flatMap(p => p.deployments.map(d => ({pool:p.name, deployment:d})));
    const reserved = new Set(deployments.flatMap(({deployment:d}) => [d.id, d.oauth_account, d.api_key_ref]).filter(Boolean));
    for (const account of remote.accounts?.data || []) reserved.add(account.name);
    config.accounts = Object.create(null);
    config.secrets = Object.create(null);
    const fields = new Set(['id','pool','provider','kind','credential','api_key','proxy','max_concurrent','rpm','enabled']);
    entries.forEach((entry, index) => {
      try {
        if (!object(entry) || Object.keys(entry).some(key => !fields.has(key))) throw Error('账号包含不支持的字段');
        const id = name(entry.id, '账号 ID');
        if (seen.has(id)) throw Error('导入内容中的账号 ID 重复');
        seen.add(id);
        const existing = editId ? deployments.find(row => row.deployment.id === editId) : null;
        if (editId && (!existing || id !== editId)) throw Error('编辑时账号 ID 不可变更，请刷新账号列表');
        if (!editId && reserved.has(id)) throw Error('账号 ID 已存在，请使用新别名；更新已有账号请使用编辑');
        const old = existing?.deployment;
        const poolName = name(entry.pool ?? existing?.pool ?? defaults.pool, '账号池名称', 80);
        const pool = config.pools.find(p => p.name === poolName);
        if (!pool) throw Error('账号池不存在，请先添加账号池');
        const provider = pool.provider;
        if (entry.provider !== undefined && entry.provider !== provider)
          throw Error('账号池的服务类型不匹配');
        if (!['openai','anthropic','xai','workbuddy'].includes(provider) || (provider === 'workbuddy' && !remote.merchant.allow_test))
          throw Error('该服务未获授权或不受支持');
        if (old && provider !== old.provider) throw Error('编辑时不可变更服务类型');
        const oldKind = old?.oauth_account && provider !== 'workbuddy' ? 'oauth' : 'api';
        const kind = entry.kind ?? (old ? oldKind : (entry.api_key !== undefined ? 'api' : 'oauth'));
        if (!['api','oauth'].includes(kind) || (old && kind !== oldKind))
          throw Error('凭证类型无效或与原账号不一致');
        if ((kind === 'api' && entry.credential !== undefined) || (kind === 'oauth' && entry.api_key !== undefined))
          throw Error('同一账号只能提供一种凭证');
        const d = old ? clone(old) : {id,provider,enabled:true};
        d.max_concurrent = integer(entry.max_concurrent ?? old?.max_concurrent ?? defaults.max_concurrent ?? 16, '并发', 1, 4096);
        d.rpm = integer(entry.rpm ?? old?.rpm ?? defaults.rpm ?? 0, 'RPM', 0, 10000000);
        d.enabled = entry.enabled ?? old?.enabled ?? true;
        if (typeof d.enabled !== 'boolean') throw Error('enabled 须为布尔值');
        const network = entry.proxy === undefined || entry.proxy === '' ? '' : proxy(entry.proxy, provider);
        if (!old && !network) throw Error('缺少 proxy：每个新账号必须单独配置网络出口代理');
        const credential = kind === 'api' ? entry.api_key : entry.credential;
        const changed = credential !== undefined;
        if (!old && !changed) throw Error('新账号必须提供凭证');
        const ref = old ? (old.oauth_account || old.api_key_ref) : id;
        const shared = old && deployments.some(({deployment:other}) => other.id !== id &&
          (old.oauth_account ? other.oauth_account === ref : !other.oauth_account && other.api_key_ref === ref));
        if (shared && (changed || (old.oauth_account && (network || d.max_concurrent !== old.max_concurrent || d.rpm !== old.rpm))))
          throw Error('此凭证被多个部署共用，不能单独更换凭证、出口或共享限额');
        if (kind === 'api') {
          if (changed) {
            if (typeof credential !== 'string' || !credential.length || credential.length > 65536 || /[\r\n]/.test(credential))
              throw Error('API key 不能为空或包含换行');
          }
          if (provider === 'workbuddy' && old?.oauth_account) {
            // Migrated Workbuddy keys live in account storage but remain API keys.
            const patch = {provider};
            if (changed) patch.credential = {api_key:credential};
            if (network) patch.proxy = network;
            const stored = (remote.accounts?.data || []).find(a => a.name === ref);
            if (d.enabled && stored?.enabled === false) patch.enabled = true;
            if (changed || network || patch.enabled) config.accounts[ref] = patch;
          } else {
            d.api_key_ref = ref;
            if (changed) config.secrets[ref] = credential;
            if (network) d.proxy = network;
          }
        } else {
          d.oauth_account = ref;
          const patch = {provider};
          if (changed) {
            if (!object(credential) || !Object.keys(credential).length) throw Error('credential 须为非空对象');
            const tokens = provider === 'workbuddy' ? [credential.api_key] : provider === 'anthropic' && credential.claudeAiOauth ?
              [credential.claudeAiOauth.accessToken] : [credential.access_token, credential.refresh_token];
            if (!tokens.some(t => typeof t === 'string' && t.length > 0)) throw Error('OAuth 凭证缺少访问令牌或刷新令牌');
            if (tokens.some(t => t !== undefined && (typeof t !== 'string' || t.length > 65536 || /[\r\n]/.test(t))))
              throw Error('凭证令牌格式无效');
            patch.credential = clone(credential);
          }
          if (network) patch.proxy = network;
          const stored = (remote.accounts?.data || []).find(a => a.name === ref);
          if (d.enabled && stored?.enabled === false) patch.enabled = true;
          if (changed || network || patch.enabled) config.accounts[ref] = patch;
        }
        if (old) {
          const previous = config.pools.find(p => p.name === existing.pool);
          previous.deployments = previous.deployments.filter(a => a.id !== id);
        }
        pool.deployments.push(d);
        const price = config.pool_pricing.find(p => p.pool === poolName);
        rows.push({id,pool:poolName,provider,kind,max_concurrent:d.max_concurrent,rpm:d.rpm,
          credential:changed ? (old ? '更换' : '新增') : '保留', proxy:network ? proxyLabel(network) : '保留',
          multiplier_millionths:price?.multiplier_millionths});
      } catch (error) { throw Error(`第 ${index + 1} 个账号：${error.message}`); }
    });
    return {config,rows};
  }
  return {parse,build,draft,pool};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = AccountConfig;
