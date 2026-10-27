"""Browser login is the authority; cached merchant credentials never grant UI access."""
import copy
import json
import unittest
from unittest.mock import patch
import httpx
from test_service import HTTPTests
from test_platform_client import login_fixture
from test_browser import FakeRouter


class AccessTests(unittest.TestCase):
    setUp = HTTPTests.setUp
    tearDown = HTTPTests.tearDown
    open_page = HTTPTests.open_page

    def prepare(self):
        self.app.accept(copy.deepcopy(FakeRouter(self.store).remote))
        self.headers = self.open_page()
        self.browser = self.client.cookies['tm_session']

    def request(self, method, path, **kwargs):
        headers = {**self.headers, 'X-Merchant-ID': self.store.enrollment()['id']}
        return self.client.request(method, path, headers=headers, **kwargs)

    def state(self):
        return self.request('GET', '/api/state').json()

    def test_saved_credentials_and_local_cookie_never_unlock_private_data(self):
        self.prepare()
        login_fixture(self.app, 'another-browser')
        value = self.state()
        self.assertFalse(value['auth']['authenticated'])
        self.assertFalse(value['credentials']['saved'])
        self.assertIsNone(value['remote'])
        self.assertEqual(value['profiles'], [])
        self.assertEqual(value['identity'], {})
        self.assertEqual(value['archive'], {})
        self.assertNotIn('seller', json.dumps(value))
        self.assertNotIn('credential_hash', json.dumps(value))
        for method, path in [('GET', '/api/credentials'), ('GET', '/api/revenue'),
                             ('GET', '/api/revenue/record?instance=old&seq=1'),
                             ('GET', '/api/records/refusals'), ('GET', '/api/records/usage'),
                             ('GET', '/api/accounts/credential-ref'), ('GET', '/api/identity'),
                             ('PUT', '/api/config'), ('PUT', '/api/settings'),
                             ('POST', '/api/sync'), ('POST', '/api/oauth/result'),
                             ('POST', '/api/accounts/refresh'),
                             ('POST', '/api/merchant-profile')]:
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, json={}).status_code, 401)
        for path in ('/account/finance', '/account/receipts'):
            self.assertEqual(self.client.get(path).status_code, 401)
        with patch.object(self.app.platform_client, 'request', side_effect=AssertionError('No remote request')):
            for method, path in [('GET', 'finance'), ('GET', 'receipts/mine'), ('POST', 'withdrawals')]:
                result = self.client.request(method, '/account-api/' + path,
                    headers={**self.headers, 'X-Account-Mode': 'merchant'}, json={})
                self.assertEqual(result.status_code, 401)

    def test_multiple_logins_are_scoped_and_logout_keeps_other_login_only(self):
        self.prepare()
        first = self.store.enrollment()['id']
        first_session=login_fixture(self.app, self.browser, 'first')
        self.assertEqual(len(self.state()['remote']['pools']), 1)
        from hashlib import sha256
        secret = 'b' * 64
        second = 'm_' + sha256(secret.encode()).hexdigest()[:32]
        self.app._api('POST', '/api/credentials', {'merchant_id': second, 'merchant_secret': secret})
        # A saved profile and an active login to another merchant are insufficient.
        self.assertFalse(self.state()['auth']['authorized'])
        self.assertEqual(self.request('GET', '/api/revenue').status_code, 401)
        second_session=login_fixture(self.app, self.browser, 'second')
        self.assertIsNone(self.state()['remote'])
        self.assertEqual(len(self.state()['auth']['accounts']), 2)
        selected = self.request('POST', '/account-api/select', json={'account_id': first_session['account']['identity_id']})
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(self.store.enrollment()['id'], first)
        self.assertEqual(len(self.state()['remote']['pools']), 1)
        self.assertEqual(self.request('POST', '/api/merchant-profile', json={'merchant_id': second}).status_code, 403)
        # Logout must revoke locally even if the platform is unreachable.
        with patch.object(self.app.platform_client, 'request', side_effect=httpx.ReadTimeout('offline')):
            self.assertEqual(self.request('POST', '/account-api/logout', json={}).status_code, 400)
        self.assertIsNone(self.state()['remote'])
        self.assertEqual(len(self.state()['auth']['accounts']),2)
        self.assertEqual(self.request('POST','/account-api/select',json={'account_id':'p_first'}).status_code,400)
        self.assertEqual(self.request('POST', '/account-api/select', json={'account_id': second_session['account']['identity_id']}).status_code, 200)
        self.assertEqual(self.store.enrollment()['id'], second)
        self.assertTrue(self.state()['auth']['authorized'])
        self.assertIsNone(self.state()['remote'])

    def test_expired_login_and_pending_account_cannot_read_cached_merchant(self):
        self.prepare()
        session = login_fixture(self.app, self.browser)
        session['expires'] = 0
        with patch.object(self.app.platform_client, 'request', side_effect=httpx.ConnectError('offline')):
            self.assertIsNone(self.state()['remote'])
            self.assertEqual(self.request('GET', '/api/revenue').status_code, 401)
        session.update(expires=10**12,restored=False,checked=10**12,merchants=set())
        value=self.state()
        self.assertTrue(value['auth']['authenticated'])
        self.assertFalse(value['auth']['authorized'])
        self.assertIsNone(value['remote'])
        self.assertEqual(self.request('GET','/api/accounts/credential-ref').status_code,401)

    def test_login_platform_setting_does_not_redirect_background_credentials(self):
        self.prepare()
        before = self.store.settings.copy()
        self.assertEqual(self.request('PUT', '/api/platform', json={'platform_url':'https://other.example'}).status_code, 200)
        self.assertEqual(self.state()['settings']['platform_url'], 'https://other.example')
        self.assertEqual(self.store.settings, before)


if __name__ == '__main__':
    unittest.main()
