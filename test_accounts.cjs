const {test} = require('node:test');
const assert = require('node:assert/strict');
const {build,parse} = require('./static/account-config.js');
const remote = () => ({merchant:{allow_test:false},settings:{revision:7,max_concurrent:16,rpm:60,tpm:1000,
  customer_defaults:{enabled:true,models:[],max_concurrent:4,rpm:0,tpm:0},
  pool_pricing:[{pool:'existing',multiplier_millionths:200000}]},
  pools:[{name:'existing',provider:'openai',strategy:'round_robin',deployments:[
    {id:'alias',provider:'openai',oauth_account:'private-ref',enabled:true,max_concurrent:8,rpm:30,weight:3},
    {id:'api-alias',provider:'openai',api_key_ref:'key-ref',proxy:'[redacted]',enabled:true,max_concurrent:4,rpm:10}]}],
  accounts:{data:[{name:'private-ref',enabled:true}]}});
const oauth = id => ({id,pool:'new-pool',provider:'openai',proxy:'socks5h://proxy.example:1080',credential:{access_token:'fixture-access',refresh_token:'fixture-refresh'}});
const defaults = {multiplier_millionths:1000000};

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
  const {config}=build(remote(),[{id:'alias',pool:'moved'}],defaults,'alias');
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

test('account aliases cannot modify object prototypes',()=>{
  const {config}=build(remote(),[oauth('__proto__'),oauth('constructor')],defaults);
  assert.equal(Object.getPrototypeOf(config.accounts),null);
  assert.ok(Object.hasOwn(config.accounts,'__proto__'));
  assert.equal(JSON.parse(JSON.stringify(config.accounts)).__proto__.provider,'openai');
  assert.equal({}.provider,undefined);
});
