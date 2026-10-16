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
    assert.equal(path.dirname(path.resolve(first)),path.resolve(base));
    assert.equal(path.dirname(path.resolve(moved)),path.resolve(base));
    fs.renameSync(first,moved);
    app=await launch(moved);page=await app.firstWindow();await page.locator('aside').waitFor();
    const restored=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.deepEqual(restored.enrollment,initial.enrollment);
    assert.ok(restored.remote);
    assert.equal(await app.evaluate(({app})=>app.getPath('userData')),path.join(moved,'data','desktop'));
    assert.deepEqual(fs.readFileSync(path.join(moved,'data','accounts.dpapi')),accountBytes);
    assert.deepEqual(fs.readFileSync(path.join(legacy,'teemerchant','settings.dpapi')),legacyBefore);
    const usage=await page.evaluate(()=>window.teemerchant.request('GET','/api/records/usage'));
    assert.ok(usage.value.data.length>0);
  }finally{
    await app?.close();
    assert.equal(path.dirname(path.resolve(base)),path.resolve(os.tmpdir()));
    fs.rmSync(base,{recursive:true,force:true});
  }
});
