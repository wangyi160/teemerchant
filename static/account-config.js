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
      if (url.protocol === 'vless:') { vless(url, value); return value; }
      if (!['http:', 'https:', 'socks5h:'].includes(url.protocol) || !url.hostname || url.hash || url.search || !['','/'].includes(url.pathname) || url.port === '0')
        throw Error();
      return value;
    } catch { throw Error('proxy 格式无效：支持 http://、https://、socks5h://，以及 TLS + TCP/XHTTP 的 vless:// 链接；VLESS 须包含 UUID、端口和 security=tls，不支持的参数会被拒绝。不支持 direct://'); }
  }
  function vless(url, value) {
    const authority = (value, port) => {
      if (!value || value.length > 253 || /[\s\\/]/.test(value) || (!port && value.includes(':'))) return false;
      const u = new URL(`https://${value}`);
      return u.hostname && !u.username && !u.password && u.pathname === '/' && !u.search && !u.hash;
    };
    if (value.length > 8192 || !value.startsWith('vless://') || !url.hostname || !url.port || url.port === '0' || url.password ||
        !['','/'].includes(url.pathname) || !/^[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(url.username)) throw Error();
    const fields = new Map();
    for (const [key,val] of url.searchParams) {
      if (!['security','encryption','type','sni','host','path','mode','fp','alpn','flow'].includes(key) ||
          fields.has(key) || /[\x00-\x1f\x7f]/.test(val)) throw Error();
      fields.set(key,val);
    }
    const get = (key, fallback) => fields.has(key) ? fields.get(key) : fallback;
    const network = get('type','tcp'), path = get('path','/'), flow = get('flow','');
    const alpn = get('alpn','').split(',').filter(Boolean);
    if (get('security','') !== 'tls' || get('encryption','none') !== 'none' ||
        !['tcp','xhttp'].includes(network) || !authority(get('sni',url.hostname),false) ||
        (get('host','') && !authority(get('host',''),true)) || !path.startsWith('/') || path.length > 2048 ||
        !['auto','stream-one','stream-up','packet-up'].includes(get('mode','auto')) ||
        !['','chrome','firefox','safari','edge','randomized'].includes(get('fp','')) ||
        !['','xtls-rprx-vision'].includes(flow) || (network === 'xhttp' && flow) ||
        alpn.length > 2 || alpn.some(p => !['h2','http/1.1'].includes(p)) ||
        (network === 'tcp' && ['host','path','mode'].some(k => fields.has(k)))) throw Error();
  }
  function proxyLabel(value) {
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
  // Matches teerouter's built-in xAI OAuth client. This cache key is an identifier,
  // never a URL to fetch or a source for routing/refresh endpoints.
  const grokCacheKey = 'https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828';
  const isGrokCache = value => object(value) && Object.keys(value).some(k => k.includes('::'));
  function normalizeCredential(provider, value) {
    if (!object(value) || !Object.keys(value).length) throw Error('credential 须为非空对象');
    let record = clone(value);
    if (isGrokCache(record)) {
      if (provider !== 'xai') throw Error('Grok 原生凭证只能导入 xai 账号池');
      if (Object.keys(record).length !== 1) throw Error('Grok 凭证文件包含多个条目，请每个账号仅保留一个认证条目');
      if (!Object.hasOwn(record,grokCacheKey)) throw Error('Grok 认证来源或客户端 ID 与 Router 当前支持的配置不匹配');
      const native = record[grokCacheKey];
      if (!object(native) || Object.keys(native).some(k => !['key','refresh_token','expires_at','scope','token_type','id_token'].includes(k)))
        throw Error('Grok 原生凭证字段无效');
      record = {access_token:native.key};
      for (const field of ['refresh_token','expires_at','scope','token_type','id_token'])
        if (Object.hasOwn(native,field)) record[field] = clone(native[field]);
      if (typeof record.access_token !== 'string' || !record.access_token)
        throw Error('Grok 原生凭证缺少 key 访问令牌');
    }
    if (provider === 'xai') {
      if (record.token_type !== undefined && (typeof record.token_type !== 'string' || record.token_type.toLowerCase() !== 'bearer'))
        throw Error('Grok 凭证 token_type 必须为 Bearer');
      if (typeof record.expires_at === 'string') {
        // Require an explicit timezone; never interpret expiry in browser local time.
        const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.exec(record.expires_at);
        if (!parts)
          throw Error('Grok expires_at 必须是带时区的 ISO 时间或 Unix 秒');
        const [year,month,day,hour,minute,second] = parts.slice(1,7).map(Number);
        if (year < 1970 || month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year,month,0)).getUTCDate() ||
            hour > 23 || minute > 59 || second > 59)
          throw Error('Grok expires_at 包含无效日期或时间');
        record.expires_at = Date.parse(record.expires_at) / 1000;
      }
      if (record.expires_at !== undefined && (typeof record.expires_at !== 'number' || !Number.isFinite(record.expires_at) ||
          record.expires_at < 0 || record.expires_at > 253402300799))
        throw Error('Grok expires_at 必须是有效的 Unix 秒');
    }
    return record;
  }
  function oauthInput(text, provider) {
    const values = parse(text), value = values[0];
    if (values.length !== 1) throw Error('此处只能填写一个账号的凭证；多个账号请使用批量导入');
    if (isGrokCache(value)) return {credential:normalizeCredential(provider,value)};
    if (value.provider !== provider || !value.credential ||
        Object.keys(value).some(k => !['provider','credential','proxy','enabled'].includes(k)))
      throw Error('请填写匹配的 provider 和 credential；xai 账号也可直接粘贴 Grok 原生凭证 JSON');
    return {...value,credential:normalizeCredential(provider,value.credential)};
  }
  function accountInput(text, provider) {
    const value = oauthInput(text, provider);
    if (Object.hasOwn(value.credential, 'api_key')) {
      if (Object.keys(value.credential).length !== 1 || typeof value.credential.api_key !== 'string' || !value.credential.api_key.trim())
        throw Error('api_key 凭证格式无效');
      return {kind:'api',api_key:value.credential.api_key,proxy:value.proxy};
    }
    return {kind:'oauth',credential:value.credential,proxy:value.proxy};
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
    if (!['openai','anthropic','xai'].includes(entry.provider))
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
  function poolModels(remote, poolName, allowed) {
    const config = draft(remote), target = config.pools.find(p => p.name === poolName);
    if (!target) throw Error('账号池不存在，请刷新后重试');
    if (allowed !== null && (!Array.isArray(allowed) || allowed.length > 4096 ||
        allowed.some(id => typeof id !== 'string' || id.length > 256 || !/^[a-zA-Z0-9._:/-]+$/.test(id) ||
          id.split('/').some(part => !part || part === '.' || part === '..')) || new Set(allowed).size !== allowed.length))
      throw Error('对外模型列表必须是有效且不重复的模型 ID');
    if (allowed === null) delete target.allowed_models;
    else target.allowed_models = [...allowed].sort();
    return config;
  }
  function rebaseAccount(base, current, entry, editId = null) {
    if (!current || base.merchant.id !== current.merchant.id)
      throw Error('商户身份已变化，当前输入已保留');
    if (current.local_sync?.state === 'remote_changed')
      throw Error('Router 上的配置与本地不同，请先核对配置；当前输入和授权凭证已保留');
    const before = base.pools.find(p => p.name === entry.pool);
    const after = current.pools.find(p => p.name === entry.pool);
    if (!before || !after || before.provider !== after.provider)
      throw Error('账号池已删除或服务类型已变化，当前输入和授权凭证已保留');
    if (editId) {
      const target = remote => {
        const pool = remote.pools.find(p => p.deployments.some(d => d.id === editId));
        const deployment = pool?.deployments.find(d => d.id === editId);
        const credential = remote.accounts?.data?.find(a => a.name === deployment?.oauth_account);
        return pool && {pool:pool.name, provider:pool.provider, deployment, enabled:credential?.enabled};
      };
      // Compare configuration only; account health and model catalogs can change
      // while the browser is completing OAuth without invalidating this edit.
      const canonical = value => Array.isArray(value) ? value.map(canonical) : object(value) ?
        Object.fromEntries(Object.keys(value).sort().map(key => [key,canonical(value[key])])) : value;
      if (!target(base) || !target(current) ||
          JSON.stringify(canonical(target(base))) !== JSON.stringify(canonical(target(current))))
        throw Error('该账号已被修改，当前输入和授权凭证已保留；请核对账号配置');
    }
    return build(current, [entry], {}, editId).config;
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
        if (isGrokCache(entry)) throw Error('请将 Grok 原生 JSON 放入该账号的 credential，并填写 id 和 proxy；单个账号也可在添加账号窗口直接粘贴');
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
        if (!['openai','anthropic','xai'].includes(provider))
          throw Error('该服务未获授权或不受支持');
        if (old && provider !== old.provider) throw Error('编辑时不可变更服务类型');
        const oldKind = old?.oauth_account ? 'oauth' : 'api';
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
        const credential = kind === 'api' ? entry.api_key : entry.credential === undefined ? undefined : normalizeCredential(provider,entry.credential);
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
          d.api_key_ref = ref;
          if (changed) config.secrets[ref] = credential;
          if (network) d.proxy = network;
        } else {
          d.oauth_account = ref;
          const patch = {provider};
          if (changed) {
            if (!object(credential) || !Object.keys(credential).length) throw Error('credential 须为非空对象');
            const tokens = provider === 'anthropic' && credential.claudeAiOauth ?
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
  return {parse,build,draft,pool,poolModels,rebaseAccount,normalizeCredential,oauthInput,accountInput,validateProxy:proxy};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = AccountConfig;
