import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import httpx
from app import Application
from credential_protocol import access_only, router_update, PROTOCOL, fingerprint
from oauth_network import OAuthError
from oauth_service import tokens, refresh
from service import LocalSessions, Server, handler
from store import Store
from test_local_accounts import Router, LocalAccountTests

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.url='https://fixture.example';self.store.settings['router']=self.url;self.store.save()
        self.router=Router(self.store);self.app=Application(self.store,self.router);self.app.accept(self.router.remote)
        self.router.remote['merchant']['credential_protocol']=PROTOCOL
        self.app.accept(self.router.remote)
        fixture=type('Fixture',(),{'app':self.app,'router':self.router})()
        config=LocalAccountTests.config(fixture)
        config.pop('secrets');d=config['pools'][0]['deployments'][0]
        d.pop('api_key_ref');proxy=d.pop('proxy');d['oauth_account']='a'
        config['accounts']={'a':{'provider':'openai','proxy':proxy,'enabled':True,
            'credential':{'access_token':'OLD','refresh_token':'PRIVATE_REFRESH','account_id':'workspace','expires_at':time.time()+30}}}
        self.app.connected=False;self.app.api('PUT','/api/config',config)
        self.book=self.store.accounts
    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def test_projection_strips_all_local_renewal_material(self):
        pending=self.book.pending(self.url);projected=router_update(pending)
        self.assertIn('PRIVATE_REFRESH',json.dumps(pending));self.assertNotIn('PRIVATE_REFRESH',json.dumps(projected))
        self.assertNotIn(b'PRIVATE_REFRESH',self.book.path.read_bytes())
        value=access_only({'claudeAiOauth':{'accessToken':'A','refreshToken':'SECRET','expiresAt':2000000000000}})
        self.assertEqual(value,{'access_token':'A','expires_at':2000000000})
        self.assertEqual(access_only({'access_token':'A','metadata':{'refresh_token':'SECRET'}}),{'access_token':'A'})

    def test_rotation_is_persisted_before_upload_and_uncertain_rotation_is_not_retried(self):
        name,account,basis=self.book.renewal_candidates(self.url)[0]
        self.book.renewal_started(self.url,name,basis)
        reloaded=Store(self.tmp.name)
        try:self.assertEqual(reloaded.accounts.renewal_candidates(self.url),[])
        finally:reloaded.db.close()
        self.book.renewal_finished(self.url,name,basis,credential={'access_token':'NEW','refresh_token':'ROTATED','account_id':'workspace','expires_at':time.time()+3600})
        self.assertNotIn(b'ROTATED',self.book.path.read_bytes())
        self.assertEqual(self.book.pending(self.url)['accounts'][name]['credential']['refresh_token'],'ROTATED')
        self.assertNotIn('ROTATED',json.dumps(router_update(self.book.pending(self.url))))

    def test_failed_local_save_prevents_transmission(self):
        with patch.object(self.book,'_write',side_effect=OSError('disk full')), patch('oauth_service.refresh') as renewal:
            with self.assertRaises(OSError):self.app.renew_credentials()
            renewal.assert_not_called()

    def test_old_router_cannot_race_the_new_merchant_refresher(self):
        self.book.data['routers'][self.url]['remote']['merchant'].pop('credential_protocol',None)
        with patch('oauth_service.refresh') as renewal:
            self.app.renew_credentials();renewal.assert_not_called()

    def test_error_quarantine_and_safe_connection_retry(self):
        name,account,basis=self.book.renewal_candidates(self.url)[0]
        self.book.renewal_started(self.url,name,basis)
        self.book.renewal_finished(self.url,name,basis,error=OAuthError('lost response',uncertain=True))
        self.assertEqual(self.book.renewal_candidates(self.url,time.time()+1000),[])
        self.book.renewal_finished(self.url,name,basis,error=OAuthError('connect failed'))
        self.assertEqual(len(self.book.renewal_candidates(self.url,time.time()+1000)),1)

    def test_owner_access_upload_is_reconciled_from_public_fingerprint(self):
        book=self.book.data['routers'][self.url];book['pending']=None
        state=copy.deepcopy(self.app.remote);state['merchant'].update(credential_protocol=PROTOCOL,runtime_id='runtime')
        state['accounts']={'data':[{'name':'a','access_sha256':hashlib.sha256(b'PREVIOUS').hexdigest()}]}
        update=self.book.access_updates(self.url,state)
        self.assertEqual(update['runtime_id'],'runtime');self.assertNotIn('refresh',json.dumps(update))
        state['accounts']['data'][0]['access_sha256']=fingerprint(book['accounts']['a']['credential'])
        self.assertIsNone(self.book.access_updates(self.url,state))

    def test_probe_does_not_send_refresh_token(self):
        seen=[]
        self.app.transport=type('T',(),{'call':lambda _,method,path,body:seen.append(body)})()
        self.app.connected=True
        self.app.api('POST','/api/accounts/test',{'pool':'openai','proxy':'http://proxy.example:80','credential':{'access_token':'A','refresh_token':'NEVER_SEND'}})
        self.assertNotIn('NEVER_SEND',json.dumps(seen))

    def test_pending_settings_do_not_block_renewal_but_manual_account_edits_do(self):
        book=self.book.data['routers'][self.url]
        state=copy.deepcopy(self.app.remote)
        state['merchant']['runtime_id']='runtime'
        state['accounts']={'data':[{'name':'a','access_sha256':hashlib.sha256(b'PREVIOUS').hexdigest()}]}
        # A manually staged credential stays behind the explicit save action.
        self.assertIsNone(self.book.access_updates(self.url,state))
        pending=book['pending']
        pending['accounts']={}
        self.assertIn('a',self.book.access_updates(self.url,state)['accounts'])
        pending['accounts']={'a':{'proxy':'http://changed.example:80'}}
        self.assertIsNone(self.book.access_updates(self.url,state))
        pending['accounts']={}
        book['config']['pools'][0]['deployments'][0]['enabled']=False
        self.assertIsNone(self.book.access_updates(self.url,state))

    def test_renewal_keeps_rotated_refresh_and_configured_proxy(self):
        for provider in ('openai','anthropic','xai'):
            old={'access_token':'A','refresh_token':'OLD_REFRESH','account_id':'workspace'}
            with patch('oauth_service.request',return_value={'access_token':'NEW','refresh_token':'ROTATED','expires_in':3600}) as call:
                result=refresh(provider,old,'socks5h://fixture:1080')
                self.assertEqual(result['refresh_token'],'ROTATED')
                self.assertEqual(call.call_args.args[2],'socks5h://fixture:1080')
            result=tokens(provider,{'access_token':'NEW','expires_in':3600},old)
            self.assertEqual(result['refresh_token'],'OLD_REFRESH')

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.app=Application(self.store)
        self.auth=LocalSessions()
        self.server=Server(('127.0.0.1',0),handler(self.app,'http://127.0.0.1',self.auth))
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.server.RequestHandlerClass=handler(self.app,self.url,self.auth)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=httpx.Client(base_url=self.url,trust_env=False,follow_redirects=False)
    def tearDown(self):
        self.client.close();self.server.shutdown();self.server.server_close();self.thread.join();self.store.db.close();self.tmp.cleanup()
    def open_page(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        session=self.auth.session(response.headers['set-cookie'])
        return {'X-CSRF-Token':session['csrf'],'Origin':self.url,'X-Merchant-ID':self.store.enrollment()['id']}
    def test_local_page_creates_session_without_password_and_apis_require_csrf(self):
        for path in ('/api/state','/api/credentials','/app.js'):
            self.assertEqual(self.client.get(path).status_code,401)
        headers=self.open_page()
        self.assertFalse(Path(self.tmp.name,'http-auth.json').exists())
        self.assertFalse(Path(self.tmp.name,'initial-password.txt').exists())
        # Platform login/logout now lives in the shared page; opening the local
        # management service itself still requires no password.
        for route,suffix in [('/account',''),('/account/login','/login'),('/account/register','/register')]:
            response=self.client.get(route)
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/#connection'+suffix)
        self.assertEqual(self.client.get('/api/credentials').status_code,403)
        self.assertEqual(self.client.get('/api/credentials',headers=headers).status_code,401)
        self.assertEqual(self.client.get('/api/credentials',headers={**headers,'Origin':'http://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={**headers,'Host':'evil.example'}).status_code,403)
        self.assertNotIn('merchant_secret',self.client.get('/api/state',headers=headers).json())
        self.assertEqual(self.client.get('/.vault-key',headers=headers).status_code,401)
        self.auth.sessions.clear()
        self.assertEqual(self.client.get('/api/credentials',headers=headers).status_code,401)
        headers=self.open_page()
        self.assertEqual(self.client.get('/api/state',headers=headers).status_code,200)
    def test_external_pages_cannot_bootstrap_local_sessions(self):
        for headers in ({'Origin':'https://evil.example'},{'Host':'evil.example'},
                        {'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'}):
            response=self.client.get('/',headers=headers)
            self.assertEqual(response.status_code,403)
            self.assertNotIn('set-cookie',response.headers)
        self.assertEqual(len(self.auth.sessions),0)
        response=self.client.get('/login')
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/')
        with self.assertRaises(ValueError):handler(self.app,'http://192.0.2.10:8910')
    def test_local_pkce_callback_is_session_scoped_and_one_time(self):
        headers=self.open_page();identity=self.store.enrollment()['id']
        from test_platform_client import login_fixture
        login_fixture(self.app,self.client.cookies['tm_session'])
        self.app.remote={'pools':[{'name':'gpt','provider':'openai'}]}
        start={'entry':{'pool':'gpt','proxy':'http://127.0.0.1:1'},'credentials_only':True,'save_refresh':True}
        result=self.client.post('/api/oauth/start',headers=headers,json=start).json()
        flow=self.app.oauth.flows[result['id']]
        self.assertIn('code_challenge=',result['auth_url'])
        bad=flow['config']['redirect']+'?code=test&state=wrong'
        self.assertEqual(self.client.post('/api/oauth/submit',headers=headers,json={'id':flow['id'],'code':bad}).status_code,400)
        from urllib.parse import urlencode
        callback=flow['config']['redirect']+'?'+urlencode({'code':'test','state':flow['state']})
        payload={'access_token':'NEW','expires_in':3600,'refresh_token':'PRIVATE','id_token':'e30.'+__import__('base64').urlsafe_b64encode(json.dumps({'https://api.openai.com/auth':{'chatgpt_account_id':'workspace'}}).encode()).decode()+'.sig'}
        with patch('oauth_service.request',return_value=payload) as exchange:
            self.assertEqual(self.client.post('/api/oauth/submit',headers=headers,json={'id':flow['id'],'code':callback}).json()['status'],'ready')
            self.assertEqual(exchange.call_count,1)
            self.assertEqual(self.client.post('/api/oauth/submit',headers=headers,json={'id':flow['id'],'code':callback}).status_code,400)
        state=self.client.post('/api/oauth/status',headers=headers,json={'id':flow['id']}).text
        self.assertNotIn('PRIVATE',state)
        self.assertEqual(self.client.post('/api/oauth/result',headers=headers,json={'id':flow['id']}).json()['credential']['refresh_token'],'PRIVATE')
        self.assertEqual(self.client.post('/api/oauth/result',headers=headers,json={'id':flow['id']}).status_code,400)

if __name__=='__main__':unittest.main()
