const {test} = require('node:test');
const assert = require('node:assert/strict');
const {build,parse,pool,normalizeCredential,oauthInput} = require('./static/account-config.js');
const remote = () => ({merchant:{allow_test:false},settings:{revision:7,max_concurrent:16,rpm:60,tpm:1000,
  customer_defaults:{enabled:true,models:[],max_concurrent:4,rpm:0,tpm:0},
  pool_pricing:[{pool:'existing',multiplier_millionths:200000},{pool:'new-pool',multiplier_millionths:1000000}]},
  pools:[{name:'existing',provider:'openai',strategy:'round_robin',deployments:[
    {id:'alias',provider:'openai',oauth_account:'private-ref',enabled:true,max_concurrent:8,rpm:30,weight:3},
    {id:'api-alias',provider:'openai',api_key_ref:'key-ref',proxy:'[redacted]',enabled:true,max_concurrent:4,rpm:10}]},{name:'new-pool',provider:'openai',deployments:[]}],
  accounts:{data:[{name:'private-ref',enabled:true}]}});
const oauth = id => ({id,pool:'new-pool',provider:'openai',proxy:'socks5h://proxy.example:1080',credential:{access_token:'fixture-access',refresh_token:'fixture-refresh'}});
const defaults = {multiplier_millionths:1000000};

test('pool creation persists an independent empty pool without rewriting credentials or other prices',()=>{
  const source=remote(), before=structuredClone(source);
  const config=pool(source,{name:'claude-team',provider:'anthropic',multiplier_millionths:250000});
  assert.deepEqual(source,before);
  assert.deepEqual(config.pools.at(-1),{name:'claude-team',provider:'anthropic',deployments:[]});
  assert.deepEqual(config.pools.slice(0,-1),source.pools);
  assert.deepEqual(config.pool_pricing.slice(0,-1),source.settings.pool_pricing);
  assert.deepEqual(config.pool_pricing.at(-1),{pool:'claude-team',multiplier_millionths:250000});
  assert.equal(config.accounts,undefined);assert.equal(config.secrets,undefined);
  assert.deepEqual(config.customers,[]);
});

test('pool edits preserve deployments, credential references, routing settings and unrelated pool prices',()=>{
  const source=remote();source.local_revision=23;
  const config=pool(source,{name:'existing',provider:'openai',multiplier_millionths:500000},'existing');
  assert.deepEqual(config.pools,source.pools);
  assert.equal(config._local_revision,23);
  assert.equal(config.expected_revision,7);
  assert.deepEqual(config.pool_pricing.find(p=>p.pool==='new-pool'),source.settings.pool_pricing[1]);
  assert.equal(config.pool_pricing.find(p=>p.pool==='existing').multiplier_millionths,500000);
  assert.equal(config.accounts,undefined);assert.equal(config.secrets,undefined);
});

test('duplicate pools, invalid prices, unauthorized providers and edits to stable identifiers are rejected',()=>{
  const source=remote(), before=structuredClone(source);
  const valid={name:'fresh',provider:'openai',multiplier_millionths:1000000};
  for (const entry of [{name:'existing'},{name:'bad/name'},{name:'a'.repeat(81)},
    {provider:'gemini'},{provider:'workbuddy'},{multiplier_millionths:-1},
    {multiplier_millionths:1.5},{multiplier_millionths:1e15+1}])
    assert.throws(()=>pool(source,{...valid,...entry}));
  assert.throws(()=>pool(source,valid,'existing'),/标识不可变更/);
  assert.throws(()=>pool(source,{...valid,name:'existing',provider:'anthropic'},'existing'),/服务类型不可变更/);
  assert.deepEqual(source,before);
  source.merchant.allow_test=true;
  assert.equal(pool(source,{...valid,provider:'workbuddy'}).pools.at(-1).provider,'workbuddy');
});

test('account add and batch import require existing pools and inherit each pool provider',()=>{
  const source=remote(), before=structuredClone(source);
  assert.throws(()=>build(source,[oauth('first'),{...oauth('second'),pool:'missing'}],defaults),/请先添加账号池/);
  assert.deepEqual(source,before);
  source.pools.push({name:'claude-team',provider:'anthropic',deployments:[]});
  const {config}=build(source,[{id:'new-openai',api_key:'fixture-one',proxy:'https://proxy.example'},
    {id:'new-claude',pool:'claude-team',credential:{claudeAiOauth:{accessToken:'fixture-two'}},proxy:'https://proxy.example'}],{pool:'existing'});
  assert.equal(config.pools[0].deployments.at(-1).provider,'openai');
  assert.equal(config.accounts['new-claude'].provider,'anthropic');
  assert.equal(config.pools.length,source.pools.length);
  assert.deepEqual(config.pool_pricing,source.settings.pool_pricing);
  assert.throws(()=>build(source,[{id:'alias',pool:'claude-team'}],{},'alias'),/服务类型/);
});

test('batch creates both credential types, binds each account and preserves existing prices/config',()=>{
  const source=remote(), before=structuredClone(source);
  const result=build(source,[oauth('first'),{...oauth('second'),pool:'existing'}],defaults);
  assert.deepEqual(source,before);
  assert.equal(result.config.expected_revision,7);
  assert.equal(result.config.pools[0].deployments.length,3);
  assert.equal(result.config.pools[1].deployments[0].oauth_account,'first');
  assert.equal(result.config.accounts.second.credential.access_token,'fixture-access');
  assert.deepEqual(result.config.pool_pricing,[{pool:'existing',multiplier_millionths:200000},{pool:'new-pool',multiplier_millionths:1000000}]);
  assert.deepEqual(result.config.customers,[]);
  const api=build(source,[{id:'key-new',pool:'existing',api_key:'fixture-key',proxy:'https://proxy.example'}],{provider:'openai'});
  assert.equal(api.config.secrets['key-new'],'fixture-key');
  assert.equal(api.config.pools[0].deployments.at(-1).api_key_ref,'key-new');
  assert.ok(!JSON.stringify(result.rows).includes('fixture-access'));
});

test('access-only replacement removes an existing refresh token instead of merging credentials',()=>{
  const credential={access_token:'FIXTURE_ACCESS_ONLY',expires_at:1893456000};
  const result=build(remote(),[{id:'alias',credential}],{},'alias');
  assert.deepEqual(result.config.accounts['private-ref'].credential,credential);
  assert.equal(result.config.accounts['private-ref'].credential.refresh_token,undefined);
});

test('blank OAuth edit preserves credential, network binding, reference and unrelated settings',()=>{
  const source=remote();
  const {config}=build(source,[{id:'alias',pool:'existing',max_concurrent:12,rpm:40}],{},'alias');
  assert.deepEqual(Object.keys(config.accounts),[]);
  assert.deepEqual(Object.keys(config.secrets),[]);
  const saved=config.pools[0].deployments.find(d=>d.id==='alias');
  assert.equal(saved.oauth_account,'private-ref');assert.equal(saved.weight,3);assert.equal(saved.max_concurrent,12);
  assert.deepEqual(config.pools[0].deployments[0],source.pools[0].deployments[1]);
});

test('OAuth proxy-only edits omit credential, credential rotation keeps the account reference',()=>{
  const proxy=build(remote(),[{id:'alias',proxy:'http://new-proxy.example:8080'}],{},'alias').config.accounts['private-ref'];
  assert.deepEqual(proxy,{provider:'openai',proxy:'http://new-proxy.example:8080'});
  const credential={access_token:'new-fixture',refresh_token:'new-refresh',account_id:'official-fixture'};
  const update=build(remote(),[{id:'alias',credential}],{},'alias');
  assert.deepEqual(update.config.accounts['private-ref'],{provider:'openai',credential});
});

test('API edits preserve a redacted proxy and rotate the original secret reference',()=>{
  const {config}=build(remote(),[{id:'api-alias',api_key:'new-fixture-key',enabled:false}],{},'api-alias');
  const account=config.pools[0].deployments.find(a=>a.id==='api-alias');
  assert.equal(account.proxy,'[redacted]');assert.equal(account.api_key_ref,'key-ref');assert.equal(account.enabled,false);
  assert.equal(config.secrets['key-ref'],'new-fixture-key');
});

test('re-enabling an OAuth deployment also restores an explicitly disabled account record',()=>{
  const source=remote();source.accounts.data[0].enabled=false;source.pools[0].deployments[0].enabled=false;
  const {config}=build(source,[{id:'alias',enabled:true}],{},'alias');
  assert.deepEqual(config.accounts['private-ref'],{provider:'openai',enabled:true});
});

test('migrated Workbuddy account storage still accepts plain API key edits',()=>{
  const source=remote();source.merchant.allow_test=true;
  source.pools[0].provider='workbuddy';source.pools[0].deployments=[{
    id:'test-account',provider:'workbuddy',oauth_account:'test-ref',enabled:true,max_concurrent:16,rpm:0}];
  const {config}=build(source,[{id:'test-account',kind:'api',api_key:'fixture-replacement',proxy:'direct://'}],{},'test-account');
  assert.deepEqual(config.accounts['test-ref'],{provider:'workbuddy',credential:{api_key:'fixture-replacement'},proxy:'direct://'});
  assert.deepEqual(Object.keys(config.secrets),[]);
  assert.equal(config.pools[0].deployments[0].api_key_ref,undefined);
  assert.equal(config.pools[0].deployments[0].oauth_account,'test-ref');
});

test('moving account preserves its identity and leaves original pool and price intact',()=>{
  const {config}=build(remote(),[{id:'alias',pool:'new-pool'}],defaults,'alias');
  assert.equal(config.pools[0].deployments.length,1);
  assert.equal(config.pools[1].deployments[0].oauth_account,'private-ref');
  assert.equal(config.pool_pricing[0].multiplier_millionths,200000);
});

test('duplicate IDs, existing IDs and credential-reference collisions fail without partial mutation',()=>{
  for(const entries of [[oauth('same'),oauth('same')],[oauth('first'),oauth('alias')],[oauth('private-ref')],[oauth('key-ref')]]){
    const source=remote(), before=structuredClone(source);
    assert.throws(()=>build(source,entries,defaults),/已存在|重复/);assert.deepEqual(source,before);
  }
});

test('invalid fields, providers, proxies, limits and conflicting credentials are rejected before submission',()=>{
  for(const override of [{provider:'workbuddy',proxy:'direct://'},{pool:'existing',provider:'anthropic'},
    {proxy:'direct://'},{proxy:'http://proxy.example/path'},{proxy:'http://proxy.example:0'},
    {max_concurrent:0},{max_concurrent:1.5},{rpm:-1},{enabled:'true'},
    {credential:{}},{api_key:'extra-key'},{unknown:'private-value'},{credential:{claudeAiOauth:{accessToken:'wrong-provider'}}}]){
    assert.throws(()=>build(remote(),[{...oauth('new'),...override}],defaults));
  }
  assert.throws(()=>build(remote(),[{id:'alias',provider:'xai'}],{},'alias'),/服务类型/);
  assert.throws(()=>build(remote(),[{id:'renamed'}],{},'alias'),/账号 ID 不可变更/);
});

test('shared credentials cannot be accidentally rotated through a single deployment',()=>{
  const source=remote();source.pools[0].deployments.push({...source.pools[0].deployments[0],id:'second-alias'});
  assert.throws(()=>build(source,[{id:'alias',credential:{access_token:'new'}}],{},'alias'),/共用/);
  assert.throws(()=>build(source,[{id:'alias',max_concurrent:12}],{},'alias'),/共用/);
});

test('JSON syntax errors and preview rows do not expose tokens or proxy passwords',()=>{
  assert.throws(()=>parse('{"credential":"SECRET_MARKER", broken'),error=>!error.message.includes('SECRET_MARKER'));
  const {rows}=build(remote(),[{...oauth('new'),proxy:'http://user:SECRET_MARKER@proxy.example'}],defaults);
  assert.ok(!JSON.stringify(rows).includes('SECRET_MARKER'));
  assert.equal(parse('\uFEFF[{"id":"a"}]')[0].id,'a');
  assert.throws(()=>parse('[]'));
});

test('each imported account requires its own proxy and cannot inherit a default',()=>{
  const source=remote(), before=structuredClone(source);
  for (const missing of [undefined,'',null,false,0,{},[]]) {
    assert.throws(()=>build(source,[oauth('first'),{...oauth('second'),proxy:missing}],
      {proxy:'https://default.example'}),/第 2 个账号.*proxy/);
    assert.deepEqual(source,before);
  }
});

test('structured proxies bind independently to OAuth and API accounts and encode auth safely',()=>{
  const auth={scheme:'socks5h',host:'proxy-1.example',port:1080,
    username:'PRIVATE_USER:@/',password:'PRIVATE_PASS:@/%?#中文'};
  const {config,rows}=build(remote(),[
    {...oauth('first'),proxy:auth},
    {id:'second',pool:'new-pool',api_key:'FIXTURE_KEY',proxy:{scheme:'https',host:'proxy-2.example',port:8443}}
  ]);
  const first=new URL(config.accounts.first.proxy);
  assert.equal(first.hostname,'proxy-1.example');assert.equal(first.port,'1080');
  assert.equal(decodeURIComponent(first.username),auth.username);
  assert.equal(decodeURIComponent(first.password),auth.password);
  assert.equal(new URL(config.pools[1].deployments[1].proxy).host,'proxy-2.example:8443');
  assert.deepEqual(rows.map(r=>r.proxy),['socks5h://***:***@proxy-1.example:1080','https://proxy-2.example:8443']);
  assert.ok(!JSON.stringify(rows).includes('PRIVATE_'));
  const ipv6=build(remote(),[{...oauth('ipv6'),proxy:{scheme:'http',host:'[::1]',port:8080}}]);
  assert.equal(ipv6.rows[0].proxy,'http://[::1]:8080');
});

test('invalid structured proxies are rejected without disclosing authentication data',()=>{
  const good={scheme:'http',host:'proxy.example',port:8080,username:'PRIVATE_USER',password:'PRIVATE_PASS'};
  for (const override of [{scheme:'socks5'},{scheme:'file'},{host:''},{host:'proxy.example/path'},
    {host:'user@proxy.example'},{host:'proxy.example?query'},{host:'proxy.example\\path'},
    {port:0},{port:65536},{port:1.5},{port:'8080'},
    {username:''},{username:null},{password:123},{unexpected:'PRIVATE_VALUE'}]) {
    assert.throws(()=>build(remote(),[{...oauth('new'),proxy:{...good,...override}}]),
      error=>error.message.includes('proxy 格式无效') && !error.message.includes('PRIVATE_'));
  }
});

test('legacy proxy URLs stay supported and direct is explicit for authorized Workbuddy only',()=>{
  const url='https://user:PRIVATE_PASS@proxy.example:8443';
  const result=build(remote(),[{...oauth('new'),proxy:url}]);
  assert.equal(result.config.accounts.new.proxy,url);
  assert.equal(result.rows[0].proxy,'https://***:***@proxy.example:8443');
  assert.throws(()=>build(remote(),[{...oauth('new'),proxy:'direct://'}]));
  const source=remote();source.merchant.allow_test=true;
  source.pools.push({name:'test-pool',provider:'workbuddy',deployments:[]});
  const entry={id:'test-account',pool:'test-pool',api_key:'FIXTURE_KEY',proxy:'direct://'};
  assert.equal(build(source,[entry]).rows[0].proxy,'direct://');
  assert.throws(()=>build(source,[{...entry,proxy:undefined}]),/缺少 proxy/);
});

test('account aliases cannot modify object prototypes',()=>{
  const {config}=build(remote(),[oauth('__proto__'),oauth('constructor')],defaults);
  assert.equal(Object.getPrototypeOf(config.accounts),null);
  assert.ok(Object.hasOwn(config.accounts,'__proto__'));
  assert.equal(JSON.parse(JSON.stringify(config.accounts)).__proto__.provider,'openai');
  assert.equal({}.provider,undefined);
});

const grokKey = 'https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828';
const grokNative = () => ({[grokKey]:{key:'FIXTURE_ACCESS',refresh_token:'FIXTURE_REFRESH',
  expires_at:'2030-01-01T00:00:00.000Z',scope:'openid offline_access',token_type:'Bearer'}});

test('Grok native cache maps key and ISO expiry while preserving refresh and scope',()=>{
  const source=grokNative(), before=structuredClone(source);
  const credential=normalizeCredential('xai',source);
  assert.deepEqual(credential,{access_token:'FIXTURE_ACCESS',refresh_token:'FIXTURE_REFRESH',
    expires_at:1893456000,scope:'openid offline_access',token_type:'Bearer'});
  assert.deepEqual(source,before);
  assert.equal(credential.token_endpoint,undefined);
  assert.equal(credential.key,undefined);
  assert.deepEqual(oauthInput(JSON.stringify(source),'xai'),{credential});
  assert.deepEqual(oauthInput(JSON.stringify({provider:'xai',credential:source}),'xai').credential,credential);
  assert.deepEqual(oauthInput(JSON.stringify({provider:'xai',credential}),'xai').credential,credential);
});

test('Grok batch import normalizes each native credential and keeps each proxy binding',()=>{
  const source=remote();source.pools.push({name:'grok',provider:'xai',deployments:[]});
  const first=grokNative(), second=grokNative();second[grokKey].key='FIXTURE_SECOND_ACCESS';
  const {config,rows}=build(source,[
    {id:'grok-001',credential:first,proxy:'http://proxy-1.example:8080'},
    {id:'grok-002',credential:second,proxy:'socks5h://proxy-2.example:1080'}],{pool:'grok'});
  assert.equal(config.accounts['grok-001'].credential.access_token,'FIXTURE_ACCESS');
  assert.equal(config.accounts['grok-002'].credential.access_token,'FIXTURE_SECOND_ACCESS');
  assert.equal(config.accounts['grok-001'].proxy,'http://proxy-1.example:8080');
  assert.equal(config.accounts['grok-002'].proxy,'socks5h://proxy-2.example:1080');
  assert.equal(config.accounts['grok-001'].credential.expires_at,1893456000);
  assert.ok(!JSON.stringify(rows).includes('FIXTURE_'));
  assert.throws(()=>build(source,[first],{pool:'grok'}),/credential.*id.*proxy/);
});

test('Grok native caches reject other providers, ambiguous entries and unsupported identities',()=>{
  const source=grokNative();
  for (const provider of ['openai','anthropic','workbuddy'])
    assert.throws(()=>normalizeCredential(provider,source),/只能导入 xai/);
  for (const value of [
    {[grokKey]:source[grokKey],'https://auth.x.ai::another':source[grokKey]},
    {'https://auth.x.ai::another':source[grokKey]},
    {'https://evil.example::PRIVATE_MARKER':source[grokKey]},
    {[grokKey]:null},{[grokKey]:{...source[grokKey],key:''}},
    {[grokKey]:{...source[grokKey],token_endpoint:'https://evil.example/PRIVATE_MARKER'}},
    {[grokKey]:{...source[grokKey],token_type:'Other'}}]) {
    assert.throws(()=>normalizeCredential('xai',value),error=>!error.message.includes('PRIVATE_MARKER'));
  }
});

test('xAI expiry parsing uses explicit timezones, rejects malformed dates and accepts expired tokens',()=>{
  const value={access_token:'FIXTURE',refresh_token:'FIXTURE_REFRESH'};
  assert.equal(normalizeCredential('xai',{...value,expires_at:'2030-01-01T08:00:00+08:00'}).expires_at,1893456000);
  assert.equal(normalizeCredential('xai',{...value,expires_at:1}).expires_at,1);
  assert.equal(normalizeCredential('xai',value).expires_at,undefined);
  for (const expiry of ['tomorrow','2030-01-01T00:00:00','2030-02-30T00:00:00Z',
    '2030-01-01T24:00:00Z',null,-1,Infinity,1893456000000])
    assert.throws(()=>normalizeCredential('xai',{...value,expires_at:expiry}),/expires_at/);
  const standard={provider:'openai',credential:{access_token:'OPENAI_FIXTURE',account_id:'account'}};
  assert.deepEqual(oauthInput(JSON.stringify(standard),'openai'),standard);
});
