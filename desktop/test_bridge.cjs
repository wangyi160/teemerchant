'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const {Backend, validateRequest} = require('./bridge.cjs');

test('only the merchant workbench operation set crosses IPC', () => {
  for (const [method, route] of [['GET','/api/state'],['GET','/api/accounts/a%20b'],['GET','/api/revenue?start=2026-01-01'],['GET','/api/revenue/record?instance=boot&seq=1'],['PUT','/api/config']]) {
    assert.equal(validateRequest(method, route).path, route);
  }
  for (const route of ['/admin/config','file:///C:/Windows/win.ini','https://example.com','/api/accounts/../../secret','/api/revenue#escape','/api/records/other']) {
    assert.throws(()=>validateRequest('GET',route));
  }
  assert.throws(()=>validateRequest('POST','/api/state'));
  assert.equal(validateRequest('POST','/api/credentials').path, '/api/credentials');
  assert.equal(validateRequest('GET','/api/credentials').path, '/api/credentials');
  assert.throws(()=>validateRequest('GET','/api/credentials?export=all'));
  assert.throws(()=>validateRequest('POST','/api/portal/bind'));
  assert.throws(()=>validateRequest('PUT','/api/settings',[]));
  assert.throws(()=>validateRequest('PUT','/api/config',{padding:'x'.repeat(16*1024*1024)}));
});

const engine = path.resolve(__dirname, '../build/resources/engine/teemerchant-engine.exe');
test('packaged private engine runs without installed Python and keeps its identity', {skip:!fs.existsSync(engine)}, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(),'teemerchant-pipe-'));
  let backend;
  try {
    backend = new Backend(engine, ['--data',directory]);
    const first = await backend.call('GET','/api/state');
    assert.equal(first.connected,false);
    assert.equal(first.settings.token,undefined);
    await assert.rejects(backend.call('GET','/admin/config'));
    await backend.close();
    backend = new Backend(engine, ['--data',directory]);
    const again = await backend.call('GET','/api/state');
    assert.deepEqual(again.enrollment, first.enrollment);
    assert.equal(again.settings.minimum_revision,0);
  } finally {
    await backend?.close();
    fs.rmSync(directory, {recursive:true,force:true});
  }
});

test('backend exit rejects outstanding requests without replay', {skip:!fs.existsSync(engine)}, async () => {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'teemerchant-exit-'));
  const backend=new Backend(engine,['--data',directory]);
  try {
    await backend.ready;
    const unavailable=new Promise(resolve=>backend.once('unavailable',resolve));
    backend.child.kill();
    await unavailable;
    await assert.rejects(backend.call('GET','/api/state'));
  } finally {
    await backend.close();
    fs.rmSync(directory,{recursive:true,force:true});
  }
});
