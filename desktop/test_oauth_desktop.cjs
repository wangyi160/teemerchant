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
    for(const [pool,provider] of [['claude','anthropic'],['grok','xai']]){
      await page.locator('[data-action=pool]').click();await page.locator('[name=name]').fill(pool);
      await page.locator('[name=provider]').selectOption(provider);await page.getByRole('button',{name:'保存并同步',exact:true}).click();await page.locator('#dialog').waitFor({state:'hidden'});
    }
    async function finishCallback(){
      const auth=new URL(await app.evaluate(()=>global.testOAuth.opened.at(-1))),redirect=new URL(auth.searchParams.get('redirect_uri'));
      redirect.search=new URLSearchParams({code:'FIXTURE_CODE',state:auth.searchParams.get('state')});
      const status=await new Promise((resolve,reject)=>http.get({host:'127.0.0.1',port:redirect.port,path:redirect.pathname+redirect.search,headers:{Host:redirect.host}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));}).on('error',reject));
      assert.equal(status,200);
    }
    for(const pool of ['openai','claude','grok']){
      await page.locator('[data-action=oauth-bind]').click();await page.locator('#oauth-setup [name=pool]').selectOption(pool);
      await page.locator('#oauth-setup [name=id]').fill(pool+'-oauth');await page.locator('#oauth-setup [name=proxy]').fill('socks5h://proxy.example:1080');
      assert.equal(await page.locator('[name=save_refresh]').isChecked(),pool!=='claude');
      if(pool==='grok')await page.locator('[name=save_refresh]').uncheck();
      await page.getByRole('button',{name:'前往官方授权',exact:true}).click();await page.locator('#oauth-open').waitFor();
      assert.equal(await page.locator('#oauth-save').count(),0);
      if(pool==='claude'){
        await page.locator('#oauth-code-form [name=code]').fill('FIXTURE_CODE#wrong');await page.getByRole('button',{name:'提交授权码',exact:true}).click();
        await page.waitForFunction(()=>document.querySelector('#oauth-progress [role=alert]').textContent.includes('不匹配'));
        await page.locator('#oauth-code-form [name=code]').fill('FIXTURE_CODE');await page.getByRole('button',{name:'提交授权码',exact:true}).click();
      }else await finishCallback();
      await page.locator('#oauth-save').waitFor();assert.doesNotMatch(await page.locator('#dialog').innerText(),/PRIVATE_DESKTOP/);
      if(pool==='openai'){
        fs.mkdirSync(path.join(ROOT,'desktop-test-results'),{recursive:true});
        await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show());
        await page.screenshot({path:path.join(ROOT,'desktop-test-results','oauth-ready.png')});
      }
      await page.locator('#oauth-save').click();await page.locator('#dialog').waitFor({state:'hidden'});
      await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('等待同步'));
    }
    const s=(await page.evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    for(const pool of ['openai','claude','grok'])assert.ok(s.remote.pools.find(p=>p.name===pool).deployments.some(d=>d.id===pool+'-oauth'));
    assert.doesNotMatch(JSON.stringify(s),/PRIVATE_DESKTOP/);
    const local=await page.evaluate(id=>window.teemerchant.request('GET','/api/accounts/grok-oauth',{},id),s.enrollment.id);
    assert.ok(local.ok);const credential=JSON.parse(local.value.credential_text).credential;assert.equal(credential.refresh_token,undefined);
    assert.equal(credential.access_token,'PRIVATE_DESKTOP_OAUTH_ACCESS');
    for(const name of ['settings.dpapi','accounts.dpapi'])assert.equal(fs.readFileSync(path.join(directory,name)).includes(Buffer.from('PRIVATE_DESKTOP')),false);
    await page.locator('[data-action=account-pool][data-id=grok]').click();await page.locator('[data-action=oauth-renew][data-id=grok-oauth]').click();
    assert.equal(await page.locator('[name=id]').inputValue(),'grok-oauth');assert.equal(await page.locator('[name=proxy]').inputValue(),'socks5h://proxy.example:1080');
    assert.equal(await page.locator('[name=save_refresh]').isChecked(),false);
    await page.getByRole('button',{name:'前往官方授权',exact:true}).click();await page.locator('#oauth-open').waitFor();
    await page.keyboard.press('Escape');await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('[data-action=oauth-renew][data-id=grok-oauth]').click();await page.getByRole('button',{name:'前往官方授权',exact:true}).click();await page.locator('#oauth-open').waitFor();
    await finishCallback();await page.locator('#oauth-save').waitFor();await page.locator('#oauth-save').click();await page.locator('#dialog').waitFor({state:'hidden'});
    assert.deepEqual(errors,[]);assert.equal(await app.evaluate(()=>global.testOAuth.exchanges),4);
    await app.close();app=await launch();const restored=(await (await app.firstWindow()).evaluate(()=>window.teemerchant.request('GET','/api/state'))).value;
    assert.equal(restored.remote.local_sync.state,'pending');assert.equal(restored.remote.pools.find(p=>p.name==='grok').deployments.length,1);
  }finally{
    await app?.close();assert.equal(path.dirname(path.resolve(directory)),path.resolve(os.tmpdir()));fs.rmSync(directory,{recursive:true,force:true});
  }
});
