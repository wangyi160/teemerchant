import json
import tempfile
import unittest
from unittest.mock import patch
import httpx
from app import Application
from store import Store


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.settings.update(platform_url='https://platform.example',credentials_saved=True,owner_secret='PRIVATE_OWNER')
        self.app=Application(self.store)
        self.identity=self.store.enrollment()['id']
        self.client=httpx.Client

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def call(self,mode,method,path,data=None,operation='test-operation'):
        return self.app.platform_client.call('browser',mode,method,path,data or {},operation,self.identity)

    def mock(self,handler):
        return patch('platform_client.httpx.Client',side_effect=lambda **kw:self.client(**kw,transport=httpx.MockTransport(handler)))

    def test_merchant_secret_only_scope_and_idempotency(self):
        def fetch(request):
            self.assertEqual(str(request.url),'https://platform.example/merchant-api/v1/withdrawals')
            self.assertEqual(request.headers['authorization'],'Bearer '+self.store.settings['token'])
            self.assertNotIn('PRIVATE_OWNER',str(request.headers)+request.content.decode())
            self.assertNotIn('cookie',request.headers)
            self.assertEqual(request.headers['idempotency-key'],'test-operation')
            return httpx.Response(201,json={'id':'1'})
        with self.mock(fetch):
            self.assertEqual(self.call('merchant','POST','withdrawals',{'amount':'1'}).status,201)
        for path in ('../api/users','merchant/credentials/reveal','https://evil.test','withdrawals/1/paid'):
            with self.assertRaises(ValueError):self.call('merchant','POST',path)

    def test_account_session_stays_local_and_is_separate_from_merchant_control(self):
        def fetch(request):
            self.assertNotIn('authorization',request.headers)
            if request.url.path.endswith('/login'):
                self.assertNotIn('cookie',request.headers)
                return httpx.Response(200,json={'csrf':'cloud-csrf','account':{'role':'merchant'}},
                                      headers={'set-cookie':'teerouter_portal='+'A'*43+'; Secure; HttpOnly'})
            self.assertEqual(request.headers['cookie'],'teerouter_portal='+'A'*43)
            self.assertEqual(request.headers['x-csrf-token'],'cloud-csrf')
            return httpx.Response(200,json={'ok':True})
        with self.mock(fetch):
            reply=self.call('account','POST','login',{'username':'merchant','password':'secret'})
            self.assertNotIn(b'cloud-csrf',reply.data)
            self.assertNotIn(b'teerouter_portal',reply.data)
            self.call('account','GET','me')
            self.call('account','POST','logout')
        self.assertFalse(self.app.platform_client.sessions)

    def test_stale_profile_wrong_role_and_redirect_cannot_bind(self):
        with self.assertRaises(ValueError):
            self.app.platform_client.call('browser','merchant','POST','withdrawals',{},'operation','different')
        with self.mock(lambda _:httpx.Response(200,json={'csrf':'secret','account':{'role':'user'}})):
            self.assertEqual(self.call('account','POST','login').status,403)
        self.assertFalse(self.app.platform_client.sessions)
        with self.mock(lambda _:httpx.Response(302,headers={'location':'https://evil.test'},json={'error':'redirect'})):
            self.assertEqual(self.call('merchant','GET','finance').status,302)


if __name__=='__main__':unittest.main()
