'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {execFileSync}=require('node:child_process');
const {_electron}=require('playwright-core');
const ROOT=path.resolve(__dirname,'..');

test('desktop OAuth browser callbacks, Claude code, reauthorization and encrypted offline persistence', {timeout:120000},async()=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'teemerchant-oauth-'));
  const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
  const packaged=process.env.TEEMERCHANT_TEST_EXE;let app;
  const launch=()=>_electron.launch({cwd:ROOT,env,...(packaged?{executablePath:packaged}:{}),args:[...(packaged?[]:[ROOT]),'--data='+directory,'--start-hidden']});
  try{
    execFileSync(path.join(ROOT,'.venv','Scripts','python.exe'),[path.join(__dirname,'seed_fixture.py'),directory]);
    app=await launch();const page=await app.firstWindow(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    // Patch dependencies in this isolated test process, never add a production IPC test bypass.
    await app.evaluate(({app})=>{
      const load=process.getBuiltinModule('node:module').createRequire(app.getAppPath()+'/package.json');
      const {OAuthController}=load('./desktop/oauth.cjs');
      const {Backend}=load('./desktop/bridge.cjs'),call=Backend.prototype.call;
      global.testProbes=[];
      Backend.prototype.call=async function(method,path,body,identity){
        if(method==='POST'&&path==='/api/accounts/test'){
          global.testProbes.push(body);
          return {ok:true,model:'fixture-text',latency_ms:12};
        }
        return call.call(this,method,path,body,identity);
      };
      const original=OAuthController.prototype.start;
      global.testOAuth={opened:[],exchanges:0};
      OAuthController.prototype.start=async function(body,identity){
        this.ports={openai:0,xai:0};this.openBrowser=async url=>global.testOAuth.opened.push(url);
        this.fetcher=async(url,init)=>{
          if(url.endsWith('openid-configuration'))return Response.json({issuer:'https://auth.x.ai',authorization_endpoint:'https://auth.x.ai/oauth2/authorize',token_endpoint:'https://auth.x.ai/oauth2/token'});
          global.testOAuth.exchanges++;
          const jwt=value=>'test.'+Buffer.from(JSON.stringify(value)).toString('base64url')+'.signature';
          return Response.json({access_token:'PRIVATE_DESKTOP_OAUTH_ACCESS',refresh_token:'PRIVATE_DESKTOP_OAUTH_REFRESH',expires_in:3600,token_type:'Bearer',
            id_token:jwt({'https://api.openai.com/auth':{chatgpt_account_id:'fixture-workspace'}})});
        };
        return original.call(this,body,identity);
      };
    });
    await page.locator('aside [data-id=accounts]').click();
    assert.equal(await page.locator('[data-action=oauth-bind]').count(),0);
    for(const [pool,provider] of [['claude','anthropic'],['grok','xai'],['workbuddy','workbuddy']]){
      await page.locator('[data-action=pool]').click();await page.locator('[name=name]').fill(pool);
      await page.locator('[name=provider]').selectOption(provider);await page.getByRole('button',{name:'保存并同步',exact:true}).click();await page.locator('#dialog').waitFor({state:'hidden'});
    }
    await page.locator('[data-action=account-pool][data-id=""]').click();
    assert.equal(await page.locator('[data-action=account]').count(),0);
    await page.locator('[data-action=account-pool][data-id=workbuddy]').click();
    await page.locator('[data-action=account]').click();
    assert.equal(await page.locator('[name=pool]').inputValue(),'workbuddy');
    assert.equal(await page.locator('[name=pool]').getAttribute('readonly'),'');
    assert.equal(await page.locator('#account-authorize').count(),0);
    assert.equal(await page.locator('[name=credential]').isVisible(),true);
    await page.keyboard.press('Escape');await page.locator('#dialog').waitFor({state:'hidden'});
    async function finishCallback(){
      const auth=new URL(await app.evaluate(()=>global.testOAuth.opened.at(-1))),redirect=new URL(auth.searchParams.get('redirect_uri'));
      redirect.search=new URLSearchParams({code:'FIXTURE_CODE',state:auth.searchParams.get('state')});
      const status=await new Promise((resolve,reject)=>http.get({host:'127.0.0.1',port:redirect.port,path:redirect.pathname+redirect.search,headers:{Host:redirect.host}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));}).on('error',reject));
      assert.equal(status,200);
    }
    for(const pool of ['openai','claude','grok']){
      await page.locator(`[data-action=account-pool][data-id=${pool}]`).click();await page.locator('[data-action=account]').click();
      assert.equal(await page.locator('[name=pool]').inputValue(),pool);
      assert.equal(await page.locator('[name=pool]').getAttribute('readonly'),'');
      assert.equal(await page.locator('select[name=pool]').count(),0);
      assert.equal(await page.locator('[name=kind]').count(),0);
      assert.equal(await page.locator('#dialog p:not([role=alert])').count(),0);
      assert.equal(await page.locator('#account-json-panel').isVisible(),false);
      assert.equal(await page.locator('#oauth-result').isVisible(),false);
      if(pool==='openai'){
        await page.locator('#account-json-tab').click();
        assert.equal(await page.locator('#account-authorize').isVisible(),false);
        await page.locator('[name=credential]').fill('{"provider":"openai","credential":{"access_token":"JSON_DRAFT"}}');
        await page.locator('#account-oauth-tab').click();
        assert.equal(await page.locator('[name=credential]').isVisible(),false);
        await page.locator('[name=enabled]').uncheck();
        fs.mkdirSync(path.join(ROOT,'desktop-test-results'),{recursive:true});
        await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show());
        await page.screenshot({path:path.join(ROOT,'desktop-test-results','account-add-oauth.png')});
      }
      assert.equal(await page.locator('[name=save_refresh]').isChecked(),pool!=='claude');
      if(pool==='grok')await page.locator('[name=save_refresh]').uncheck();
      // Authorization is independent of required alias/proxy fields and never saves.
      await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();
      assert.equal(await page.locator('[name=credential]').isVisible(),false);
      assert.equal(await page.getByRole('button',{name:'保存并同步',exact:true}).isDisabled(),true);
      await page.locator('[name=id]').fill(pool+'-oauth');await page.locator('[name=proxy]').fill('socks5h://proxy.example:1080');
      if(pool==='claude'){
        await page.locator('[name=code]').fill('FIXTURE_CODE#wrong');await page.locator('#oauth-submit').click();
        await page.waitForFunction(()=>document.querySelector('#dialog [role=alert]').textContent.includes('不匹配'));
        await page.locator('[name=code]').fill('FIXTURE_CODE');await page.locator('#oauth-submit').click();
      }else{
        if(pool==='openai')await page.locator('#account-json-tab').click();
        await finishCallback();
      }
      await page.waitForFunction(()=>document.querySelector('[name=oauth_credential]').value.includes('PRIVATE_DESKTOP_OAUTH_ACCESS'));
      if(pool==='openai'){
        assert.equal(await page.locator('#account-oauth-panel').isVisible(),false);
        assert.equal(JSON.parse(await page.locator('[name=credential]').inputValue()).credential.access_token,'JSON_DRAFT');
        await page.locator('#account-oauth-tab').click();
      }
      const draft=JSON.parse(await page.locator('[name=oauth_credential]').inputValue());
      assert.equal(draft.credential.refresh_token,pool==='openai'?'PRIVATE_DESKTOP_OAUTH_REFRESH':undefined);
      assert.ok(draft.credential.expires_at);
      const before=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
      assert.equal(before.remote.pools.find(p=>p.name===pool).deployments.some(d=>d.id===pool+'-oauth'),false);
      if(pool==='openai'){
        assert.equal(draft.credential.account_id,'fixture-workspace');
        draft.credential.access_token='PRIVATE_EDITED_ACCESS';
        await page.locator('[name=oauth_credential]').fill(JSON.stringify(draft,null,2));
        await page.locator('#account-json-tab').click();
        assert.equal(await page.locator('[name=oauth_credential]').isVisible(),false);
        assert.equal(JSON.parse(await page.locator('[name=credential]').inputValue()).credential.access_token,'JSON_DRAFT');
        await page.screenshot({path:path.join(ROOT,'desktop-test-results','account-add-json.png')});
        await page.locator('#account-oauth-tab').click();
        assert.equal(JSON.parse(await page.locator('[name=oauth_credential]').inputValue()).credential.access_token,'PRIVATE_EDITED_ACCESS');
      }
      if(pool==='openai'){
        fs.mkdirSync(path.join(ROOT,'desktop-test-results'),{recursive:true});
        await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show());
        await page.screenshot({path:path.join(ROOT,'desktop-test-results','oauth-ready.png')});
      }
      assert.equal(await page.getByRole('button',{name:'保存并同步',exact:true}).isDisabled(),true);
      await page.locator('#account-test').click();
      await page.waitForFunction(()=>document.querySelector('#account-test-status').textContent.includes('通过'));
      const probe=await app.evaluate(()=>global.testProbes.at(-1));
      assert.equal(probe.pool,pool);
      assert.equal(probe.credential.access_token,pool==='openai'?'PRIVATE_EDITED_ACCESS':'PRIVATE_DESKTOP_OAUTH_ACCESS');
      assert.equal(probe.proxy,'socks5h://proxy.example:1080');
      if(pool==='grok'){
        // Reproduce a configuration change after OAuth has filled the token editor.
        const changed=await page.evaluate(async()=>{
          const current=(await window.teemerchant.request('GET','/api/state')).value;
          const config=AccountConfig.draft(current.remote);
          config.pools.find(p=>p.name==='claude').allowed_models=['fixture-current-model'];
          config.pools.find(p=>p.name==='claude').deployments[0].rpm=19;
          return window.teemerchant.request('PUT','/api/config',config,current.enrollment.id);
        });
        assert.equal(changed.ok,true);
      }
      await page.getByRole('button',{name:'保存并同步',exact:true}).click();await page.locator('#dialog').waitFor({state:'hidden'});
      await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('等待同步'));
    }
    const s=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.deepEqual(s.remote.pools.find(p=>p.name==='claude').allowed_models,['fixture-current-model']);
    assert.equal(s.remote.pools.find(p=>p.name==='claude').deployments[0].rpm,19);
    for(const pool of ['openai','claude','grok'])assert.ok(s.remote.pools.find(p=>p.name===pool).deployments.some(d=>d.id===pool+'-oauth'));
    assert.equal(s.remote.pools.find(p=>p.name==='openai').deployments.find(d=>d.id==='openai-oauth').enabled,false);
    assert.doesNotMatch(JSON.stringify(s),/PRIVATE_DESKTOP/);
    const edited=await page.evaluate(id=>window.teemerchant.request('GET','/api/accounts/openai-oauth',{},id),s.enrollment.id);
    assert.equal(JSON.parse(edited.value.credential_text).credential.access_token,'PRIVATE_EDITED_ACCESS');
    const local=await page.evaluate(id=>window.teemerchant.request('GET','/api/accounts/grok-oauth',{},id),s.enrollment.id);
    assert.ok(local.ok);const credential=JSON.parse(local.value.credential_text).credential;assert.equal(credential.refresh_token,undefined);
    assert.equal(credential.access_token,'PRIVATE_DESKTOP_OAUTH_ACCESS');
    for(const name of ['settings.dpapi','accounts.dpapi'])assert.equal(fs.readFileSync(path.join(directory,name)).includes(Buffer.from('PRIVATE_DESKTOP')),false);
    await page.locator('[data-action=account-pool][data-id=grok]').click();await page.locator('[data-action=oauth-renew][data-id=grok-oauth]').click();
    assert.equal(await page.locator('[name=id]').inputValue(),'grok-oauth');assert.equal(await page.locator('[name=proxy]').inputValue(),'socks5h://proxy.example:1080');
    assert.equal(await page.locator('[name=save_refresh]').isChecked(),false);
    const previous=await page.locator('[name=oauth_credential]').inputValue();
    await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();
    await page.locator('#oauth-cancel').click();
    await page.waitForFunction(()=>!document.querySelector('#account-authorize').disabled);
    assert.equal(await page.locator('[name=oauth_credential]').inputValue(),previous);
    await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();
    await page.keyboard.press('Escape');await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('[data-action=oauth-renew][data-id=grok-oauth]').click();await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();
    await finishCallback();await page.waitForFunction(()=>document.querySelector('#oauth-progress [role=status]')?.textContent==='已授权');
    await page.locator('#account-test').click();
    await page.waitForFunction(()=>document.querySelector('#account-test-status').textContent.includes('通过'));
    await page.getByRole('button',{name:'保存并同步',exact:true}).click();await page.locator('#dialog').waitFor({state:'hidden'});
    // Discarding a successful authorization still leaves the account store untouched.
    await page.locator('[data-action=account-pool][data-id=openai]').click();await page.locator('[data-action=account]').click();
    await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();await finishCallback();
    await page.waitForFunction(()=>document.querySelector('#oauth-progress [role=status]')?.textContent==='已授权');
    const revision=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value.remote.local_revision;
    await page.locator('[name=id]').fill('discarded');await page.keyboard.press('Escape');await page.locator('#dialog').waitFor({state:'hidden'});
    assert.equal((await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value.remote.local_revision,revision);
    await page.locator('[data-action=account]').click();
    await page.locator('#account-authorize').click();await page.locator('#oauth-open').waitFor();
    await page.locator('#account-json-tab').click();
    await page.locator('[name=id]').fill('manual-json');await page.locator('[name=proxy]').fill('https://proxy.example');
    await page.locator('[name=credential]').fill('{"provider":"openai","credential":{"access_token":"PRIVATE_MANUAL_JSON"}}');
    await page.locator('#account-test').click();
    await page.waitForFunction(()=>document.querySelector('#account-test-status').textContent.includes('通过'));
    assert.equal((await app.evaluate(()=>global.testProbes.at(-1))).credential.access_token,'PRIVATE_MANUAL_JSON');
    await page.getByRole('button',{name:'保存并同步',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#dialog').open||document.querySelector('#dialog [role=alert]')?.textContent);
    assert.equal(await page.locator('#dialog').isVisible(),false,await page.evaluate(()=>document.querySelector('#dialog [role=alert]')?.textContent||''));
    const manual=await page.evaluate(id=>window.teemerchant.request('GET','/api/accounts/manual-json',{},id),s.enrollment.id);
    assert.equal(JSON.parse(manual.value.credential_text).credential.access_token,'PRIVATE_MANUAL_JSON');
    assert.deepEqual(errors,[]);assert.equal(await app.evaluate(()=>global.testOAuth.exchanges),5);
    await app.close();app=await launch();const restored=(await (await app.firstWindow()).evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.equal(restored.remote.local_sync.state,'pending');assert.equal(restored.remote.pools.find(p=>p.name==='grok').deployments.length,1);
  }finally{
    await app?.close();assert.equal(path.dirname(path.resolve(directory)),path.resolve(os.tmpdir()));fs.rmSync(directory,{recursive:true,force:true});
  }
});
