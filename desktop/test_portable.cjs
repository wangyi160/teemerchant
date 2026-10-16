'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFileSync}=require('node:child_process');
const {_electron}=require('playwright-core');
const ROOT=path.resolve(__dirname,'..');

test('portable data follows executable across directory moves and ignores AppData', {timeout:120000}, async()=>{
  const base=fs.mkdtempSync(path.join(os.tmpdir(),'teemerchant-portable-'));
  const first=path.join(base,'first'),moved=path.join(base,'moved'),cwd=path.join(base,'unrelated');
  const legacy=path.join(base,'legacy');
  fs.mkdirSync(cwd);fs.mkdirSync(legacy);
  const source=path.dirname(process.env.TEEMERCHANT_TEST_EXE||path.join(ROOT,'release','win-unpacked','teemerchant.exe'));
  fs.cpSync(source,first,{recursive:true,filter:p=>path.relative(source,p).split(path.sep)[0]!=='data'});
  const env={...process.env,LOCALAPPDATA:legacy};delete env.ELECTRON_RUN_AS_NODE;
  let app;
  const launch=dir=>_electron.launch({executablePath:path.join(dir,'teemerchant.exe'),args:['--start-hidden'],cwd,env});
  try{
    // A different existing AppData identity must never be picked up implicitly.
    execFileSync(path.join(ROOT,'.venv','Scripts','python.exe'),[path.join(__dirname,'seed_fixture.py'),path.join(legacy,'teemerchant')]);
    const legacyBefore=fs.readFileSync(path.join(legacy,'teemerchant','settings.dpapi'));
    app=await launch(first);
    let page=await app.firstWindow();await page.locator('aside').waitFor();
    const initial=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.equal(initial.remote,null);
    assert.equal(await app.evaluate(({app})=>app.getPath('userData')),path.join(first,'data','desktop'));
    assert.equal(await app.evaluate(({app})=>app.getPath('sessionData')),path.join(first,'data','desktop'));
    assert.ok(fs.existsSync(path.join(first,'data','settings.dpapi')));
    assert.equal(fs.existsSync(path.join(cwd,'data')),false);
    await app.close();app=null;
    // Add offline pool/account and archive fixtures to exercise actual vault relocation.
    execFileSync(path.join(ROOT,'.venv','Scripts','python.exe'),[path.join(__dirname,'seed_fixture.py'),path.join(first,'data')]);
    const accountBytes=fs.readFileSync(path.join(first,'data','accounts.dpapi'));
    app=await launch(first);page=await app.firstWindow();await page.locator('aside').waitFor();
    const policy=path.join(base,'approved-policy.json');fs.writeFileSync(policy,'{"fixture":true}');
    assert.equal((await page.evaluate(policy=>window.teemerchant.request('PUT','/api/settings',
      {router:'https://fixture.invalid',tdx_policy:policy,snp_policy:''}),policy)).ok,true);
    const secret='c'.repeat(64),id='m_'+require('node:crypto').createHash('sha256').update(secret).digest('hex').slice(0,32);
    assert.equal((await page.evaluate(body=>window.teemerchant.request('POST','/api/credentials',body),
      {merchant_id:id,merchant_secret:secret})).ok,true);
    const second=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.equal(second.enrollment.id,id);assert.equal(second.remote,null);
    assert.equal(path.isAbsolute(second.settings.tdx_policy),false);
    await app.close();app=null;
    assert.equal(path.dirname(path.resolve(first)),path.resolve(base));
    assert.equal(path.dirname(path.resolve(moved)),path.resolve(base));
    fs.renameSync(first,moved);
    app=await launch(moved);page=await app.firstWindow();await page.locator('aside').waitFor();
    const restored=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.equal(restored.enrollment.id,id);
    assert.equal(restored.remote,null);
    assert.equal(fs.readFileSync(path.join(moved,'data','merchants',id,restored.settings.tdx_policy),'utf8'),'{"fixture":true}');
    assert.equal(await app.evaluate(({app})=>app.getPath('userData')),path.join(moved,'data','desktop'));
    assert.deepEqual(fs.readFileSync(path.join(moved,'data','accounts.dpapi')),accountBytes);
    assert.deepEqual(fs.readFileSync(path.join(legacy,'teemerchant','settings.dpapi')),legacyBefore);
    assert.equal((await page.evaluate(merchant_id=>window.teemerchant.request('POST','/api/merchant-profile',{merchant_id}),initial.enrollment.id)).ok,true);
    const original=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.deepEqual(original.enrollment,initial.enrollment);assert.ok(original.remote);
    const usage=await page.evaluate(()=>window.teemerchant.request('GET','/api/records/usage'));
    assert.ok(usage.value.data.length>0);
  }finally{
    await app?.close();
    assert.equal(path.dirname(path.resolve(base)),path.resolve(os.tmpdir()));
    fs.rmSync(base,{recursive:true,force:true});
  }
});
