import json
import hashlib
import tempfile
import unittest
import time
from unittest.mock import patch
import httpx
from app import Application
from store import Store


def login_fixture(app, browser, username='seller'):
    """Exercise the real login grant path using an isolated platform response."""
    identity = app.store.enrollment()['id']
    app.store.settings['credentials_saved'] = True
    account = {'id': 'p_' + username, 'role': 'merchant', 'username': username, 'name': username}
    view = {'account': account, 'merchant_credentials_available': True,
            'applications': [{'kind': 'merchant', 'status': 'approved', 'subject': identity}]}
    response = (200, dict(view, csrf='fixture-csrf'), {'set-cookie': 'teerouter_portal=' + 'A' * 43})
    with patch.object(app.platform_client, 'request', return_value=response):
        result = app.platform_client.call(browser, 'account', 'POST', 'login', {}, '', identity)
    assert result.status == 200 and json.loads(result.data)['local_connected']
    return app.platform_client.current(browser)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name)
        self.store.settings.update(platform_url='https://platform.example',credentials_saved=True,owner_secret='PRIVATE_OWNER')
        self.store.save()
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
        login_fixture(self.app, 'browser')
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

    def test_logged_out_account_operations_do_not_contact_platform(self):
        with self.mock(lambda _: self.fail('No platform session should be rejected locally')):
            for method, path in [('GET', 'me'), ('POST', 'password'), ('POST', 'logout'), ('POST', 'bind')]:
                self.assertEqual(self.call('account', method, path).status, 401)

    def test_account_session_stays_local_and_is_separate_from_merchant_control(self):
        view, _ = self.account_fixture(False)
        def fetch(request):
            self.assertNotIn('authorization',request.headers)
            if request.url.path.endswith('/login'):
                self.assertNotIn('cookie',request.headers)
                return httpx.Response(200,json={**view,'csrf':'cloud-csrf'},
                                      headers={'set-cookie':'teerouter_portal='+'A'*43+'; Secure; HttpOnly'})
            self.assertEqual(request.headers['cookie'],'teerouter_portal='+'A'*43)
            self.assertEqual(request.headers['x-csrf-token'],'cloud-csrf')
            return httpx.Response(200,json=view)
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
        login_fixture(self.app, 'browser')
        with self.mock(lambda _:httpx.Response(302,headers={'location':'https://evil.test'},json={'error':'redirect'})):
            self.assertEqual(self.call('merchant','GET','finance').status,302)

    def account_fixture(self, approved=True):
        secret = 'b' * 64
        merchant_id = 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32]
        return {'account': {'id': 'p_seller', 'role': 'merchant', 'username': 'seller', 'name': 'Seller'},
                'applications': [{'kind': 'merchant', 'status': 'approved' if approved else 'pending',
                                  'subject': merchant_id if approved else 'p_seller'}],
                'merchant': {'id': merchant_id} if approved else None,
                'merchant_credentials_available': approved}, {'merchant_id': merchant_id, 'merchant_secret': secret}

    def test_login_binds_and_persists_username_without_exposing_credentials(self):
        view, credentials = self.account_fixture()
        first = self.identity
        requests = []
        def fetch(request):
            requests.append(request.url.path)
            self.assertNotIn('PRIVATE_OWNER', str(request.headers) + request.content.decode())
            if request.url.path.endswith('/login'):
                return httpx.Response(200, json={**view, 'csrf': 'private-csrf'},
                    headers={'set-cookie': 'teerouter_portal=' + 'A' * 43})
            self.assertEqual(request.headers['cookie'], 'teerouter_portal=' + 'A' * 43)
            self.assertNotIn('authorization', request.headers)
            if request.url.path.endswith('/reveal'):
                self.assertEqual(request.headers['x-csrf-token'], 'private-csrf')
                return httpx.Response(200, json=credentials)
            return httpx.Response(200, json=view)
        with self.mock(fetch):
            reply = self.call('account', 'POST', 'login', {'username': 'seller', 'password': 'not-saved'})
            result = json.loads(reply.data)
            self.assertTrue(result['local_connected'])
            self.assertEqual(result['local_merchant_id'], credentials['merchant_id'])
            self.assertNotIn(credentials['merchant_secret'].encode(), reply.data)
            self.assertNotIn(b'private-csrf', reply.data)
            self.identity = credentials['merchant_id']
            self.assertEqual(self.call('account', 'GET', 'me').status, 200)
        self.assertEqual(requests.count('/portal/api/merchant/credentials/reveal'), 1)
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(reopened.platform_identity()['username'], 'seller')
            self.assertEqual(reopened.credentials()['merchant_id'], self.identity)
            self.assertNotIn('not-saved', json.dumps(reopened.settings))
        finally:
            reopened.db.close()
        self.assertEqual(len(self.store.profiles()), 2)
        self.app.api('POST', '/api/merchant-profile', {'merchant_id': first})
        self.identity = first
        self.assertEqual(self.store.settings['owner_secret'], 'PRIVATE_OWNER')
        self.assertEqual(self.store.platform_identity(), {})
        with self.mock(lambda _: self.fail('Stale login must not switch the selected merchant back')):
            self.assertEqual(self.call('account', 'GET', 'me').status, 401)

    def test_pending_registration_binds_when_approved_and_retries_failed_delivery(self):
        pending, credentials = self.account_fixture(False)
        approved, _ = self.account_fixture(True)
        current, fail_delivery = pending, True
        def fetch(request):
            if request.url.path.endswith('/register'):
                return httpx.Response(201, json={**pending, 'csrf': 'csrf'},
                    headers={'set-cookie': 'teerouter_portal=' + 'A' * 43})
            if request.url.path.endswith('/reveal'):
                if fail_delivery:
                    raise httpx.ReadTimeout('delivery interrupted')
                return httpx.Response(200, json=credentials)
            return httpx.Response(200, json=current)
        with self.mock(fetch):
            response = self.call('account', 'POST', 'register', {'role': 'merchant'})
            self.assertFalse(json.loads(response.data)['local_connected'])
            self.assertEqual(self.store.enrollment()['id'], self.identity)
            current = approved
            response = self.call('account', 'GET', 'me')
            self.assertIn('local_connection_error', json.loads(response.data))
            self.assertEqual(self.store.enrollment()['id'], self.identity)
            fail_delivery = False
            response = self.call('account', 'GET', 'me')
            self.assertTrue(json.loads(response.data)['local_connected'])
            self.assertEqual(self.store.enrollment()['id'], credentials['merchant_id'])

    def test_existing_profile_uses_verified_username_and_legacy_name_is_not_a_username(self):
        account = self.account_fixture()[0]['account']
        profile = {'account': account, 'merchant': {'id': self.identity}, 'legacy': False}
        def fetch(request):
            self.assertEqual(request.url.path, '/merchant-api/v1/profile')
            self.assertNotIn('PRIVATE_OWNER', str(request.headers))
            return httpx.Response(200, json=profile)
        with self.mock(fetch):
            self.assertEqual(self.app.api('GET', '/api/identity', {})['username'], 'seller')
            self.assertEqual(self.app.api('GET', '/api/state', {})['identity']['username'], 'seller')
            profile['merchant']['id'] = 'm_' + '0' * 32
            with self.assertRaises(ValueError): self.app.api('GET', '/api/identity', {})
            self.assertEqual(self.store.platform_identity()['username'], 'seller')
            profile['merchant']['id'] = self.identity
            profile['legacy'] = True
            self.assertEqual(self.app.api('GET', '/api/identity', {})['username'], '')
        self.store.settings['platform_url'] = 'https://another.example'
        self.assertEqual(self.store.platform_identity(), {})

    def test_automatic_binding_rejects_another_merchants_credential(self):
        view, credentials = self.account_fixture()
        credentials = dict(credentials, merchant_id=self.identity, merchant_secret=self.store.settings['token'])
        def fetch(request):
            if request.url.path.endswith('/login'):
                return httpx.Response(200, json={**view, 'csrf': 'csrf'},
                    headers={'set-cookie': 'teerouter_portal=' + 'A' * 43})
            return httpx.Response(200, json=credentials)
        with self.mock(fetch):
            reply = self.call('account', 'POST', 'login')
            self.assertIn('local_connection_error', json.loads(reply.data))
            self.assertEqual(self.store.enrollment()['id'], self.identity)
            self.assertEqual(self.store.platform_identity(), {})


if __name__=='__main__':unittest.main()
