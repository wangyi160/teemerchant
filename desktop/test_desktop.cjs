'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const {_electron} = require('playwright-core');
const ROOT = path.resolve(__dirname, '..');

test('desktop account workflow, persistent identity, IPC isolation and tray lifecycle', {timeout:120000}, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'teemerchant-desktop-'));
  const env = {...process.env}; delete env.ELECTRON_RUN_AS_NODE;
  const packaged = process.env.TEEMERCHANT_TEST_EXE;
  const launch = () => _electron.launch({cwd:ROOT, env, ...(packaged ? {executablePath:packaged} : {}),
    args:[...(packaged?[]:[ROOT]), '--data='+directory, '--start-hidden']});
  let app;
  try {
    execFileSync(path.join(ROOT,'.venv','Scripts','python.exe'),[path.join(__dirname,'seed_fixture.py'),directory]);
    app = await launch();
    let page = await app.firstWindow();
    const errors=[]; page.on('pageerror',error=>errors.push(error.message));
    await page.locator('aside [data-id=accounts]').click();
    assert.equal(await page.evaluate(()=>typeof require),'undefined');
    assert.equal(await page.evaluate(()=>typeof process),'undefined');
    const prefs=await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
    assert.equal(prefs.contextIsolation,true); assert.equal(prefs.sandbox,true); assert.equal(prefs.nodeIntegration,false);
    const denied=await page.evaluate(()=>window.teemerchant.request('GET','/admin/config'));
    assert.equal(denied.ok,false);
    assert.equal(await page.evaluate(()=>fetch('https://example.com').then(()=>false,()=>true)),true);
    const initial=await page.evaluate(()=>window.teemerchant.request('GET','/api/state'));
    assert.equal(initial.value.settings.transport,'builtin');
    // No HTTP listener is created by Electron or its private management engine.
    const pid=await app.evaluate(()=>process.pid);
    // Playwright itself starts inspector/CDP listeners; inspect only the private engine here.
    const enginePorts=execFileSync('powershell.exe',['-NoProfile','-Command',
      `$pids=@(Get-CimInstance Win32_Process|Where-Object {$_.ParentProcessId -eq ${pid} -and $_.Name -eq 'teemerchant-engine.exe'}|ForEach-Object {$_.ProcessId}); if($pids.Count -ne 1){throw 'private engine missing'}; @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|Where-Object {$pids -contains $_.OwningProcess}).Count`],{encoding:'utf8',windowsHide:true});
    assert.equal(Number(enginePorts.trim()),0);
    await page.locator('[data-action=pool]').click();
    await page.locator('[name=name]').fill('desktop-pool');
    await page.locator('[name=provider]').selectOption('xai');
    await page.locator('[name=multiplier]').fill('0.3');
    await page.getByRole('button',{name:'保存并同步',exact:true}).click();
    await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('[data-action=account]').click();
    await page.locator('[name=id]').fill('desktop-account');
    await page.locator('[name=credential]').fill('FAKE_DESKTOP_API_KEY');
    await page.locator('[name=proxy]').fill('http://proxy.example:8080');
    await page.getByRole('button',{name:'保存并同步',exact:true}).click();
    await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('[data-action=edit-account][data-id=desktop-account]').click();
    assert.equal(await page.locator('[name=credential]').inputValue(),'FAKE_DESKTOP_API_KEY');
    await page.locator('[name=max_concurrent]').fill('3');
    await page.getByRole('button',{name:'保存并同步',exact:true}).click();
    await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('[data-action=import-accounts]').click();
    await page.locator('[name=entries]').fill(JSON.stringify([
      {id:'batch-a',api_key:'FAKE_BATCH_A',proxy:{scheme:'socks5h',host:'a.example',port:1080}},
      {id:'batch-b',api_key:'FAKE_BATCH_B',proxy:{scheme:'http',host:'b.example',port:8080}}
    ]));
    await page.locator('#import-preview').click();
    assert.match(await page.locator('#import-review').innerText(),/2 个账号/);
    assert.doesNotMatch(await page.locator('#import-review').innerText(),/FAKE_BATCH/);
    await page.getByRole('button',{name:'确认导入',exact:true}).click();
    await page.locator('#dialog').waitFor({state:'hidden'});
    const saved=await page.evaluate(()=>window.teemerchant.request('GET','/api/state'));
    const pool=saved.value.remote.pools.find(p=>p.name==='desktop-pool');
    assert.equal(pool.deployments.length,3);
    assert.equal(pool.deployments.find(a=>a.id==='desktop-account').max_concurrent,3);
    assert.equal(saved.value.remote.local_sync.state,'pending');
    assert.doesNotMatch(JSON.stringify(saved),/FAKE_DESKTOP_API_KEY|FAKE_BATCH_A/);
    for(const name of ['settings.dpapi','accounts.dpapi']) {
      assert.equal(fs.readFileSync(path.join(directory,name)).includes(Buffer.from('FAKE_DESKTOP_API_KEY')),false);
    }
    await page.locator('aside [data-id=revenue]').click();
    await page.locator('.revenue-cards').waitFor();
    assert.match(await page.locator('.revenue-cards').innerText(),/\$0\.000050/);
    await page.locator('aside [data-id=accounts]').click();
    fs.mkdirSync(path.join(ROOT,'desktop-test-results'),{recursive:true});
    await page.screenshot({path:path.join(ROOT,'desktop-test-results',packaged?'packaged.png':'development.png')});
    await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show());
    await page.locator('[data-action=desktop-minimize]').click();
    assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].isVisible()),false);
    assert.deepEqual(errors,[]);
    await app.close(); app=await launch(); page=await app.firstWindow();
    await page.locator('aside [data-id=accounts]').click();
    const restored=await page.evaluate(()=>window.teemerchant.request('GET','/api/state'));
    assert.deepEqual(restored.value.enrollment,initial.value.enrollment);
    assert.equal(restored.value.remote.pools.find(p=>p.name==='desktop-pool').deployments.length,3);
    assert.equal(restored.value.remote.local_sync.state,'pending');
  } finally {
    await app?.close();
    // This directory comes directly from mkdtemp under the OS temporary directory.
    fs.rmSync(directory,{recursive:true,force:true});
  }
});
