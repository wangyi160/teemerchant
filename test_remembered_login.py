import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from app import Application
from platform_client import PlatformClient
from remembered_login import LIFETIME, BROWSER_LIFETIME
from service import LocalSessions
from store import Store
from test_platform_client import login_fixture


class RememberedLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.settings.update(credentials_saved=True, platform_url='https://platform.example')
        self.store.save()
        self.app = Application(self.store)
        self.local = LocalSessions(self.temp.name)
        self.browser = self.local.create()
        self.session = login_fixture(self.app, self.browser)
        self.view = json.loads(json.dumps(self.session['view']))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def restart(self):
        self.app.platform_client = PlatformClient(self.app)
        self.local = LocalSessions(self.temp.name)
        self.assertEqual(self.local.session('tm_session=' + self.browser)['id'], self.browser)

    def test_restart_revalidates_then_restores_only_matching_browser(self):
        for name in ('browser-sessions.dpapi', 'platform-sessions.dpapi'):
            data = Path(self.temp.name, name).read_bytes()
            self.assertNotIn(self.browser.encode(), data)
            self.assertNotIn(b'teerouter_portal=', data)
            self.assertNotIn(b'fixture-csrf', data)
        self.restart()
        client = self.app.platform_client
        self.assertFalse(client.authorized(self.browser))
        with patch.object(client, 'request', return_value=(200, self.view, {})) as request:
            state = client.snapshot(self.browser)
            self.assertTrue(state['auth']['authorized'])
            self.assertEqual(state['auth']['account']['username'], 'seller')
            request.assert_called_once()
            self.assertTrue(request.call_args.args[2].endswith('/portal/api/me'))
            self.assertFalse(client.snapshot(self.local.create())['auth']['authenticated'])
            self.assertEqual(request.call_count, 1)

    def test_outage_keeps_login_but_no_private_data_until_verified(self):
        self.restart()
        client = self.app.platform_client
        with patch.object(client, 'request', side_effect=httpx.ConnectError('offline')):
            state = client.snapshot(self.browser)
        self.assertTrue(state['auth']['authenticated'])
        self.assertTrue(state['auth']['restoring'])
        self.assertFalse(state['auth']['authorized'])
        self.assertIsNone(state['remote'])
        self.restart()
        client = self.app.platform_client
        with patch.object(client, 'request', return_value=(200, self.view, {})):
            self.assertTrue(client.snapshot(self.browser)['auth']['authorized'])

    def test_expired_or_revoked_session_cannot_return_after_restart(self):
        self.restart()
        client = self.app.platform_client
        with patch.object(client, 'request', return_value=(401, {'error': 'login_required'}, {})):
            self.assertFalse(client.snapshot(self.browser)['auth']['authenticated'])
        self.restart()
        self.assertIsNone(self.app.platform_client.current(self.browser))

    def test_offline_logout_is_persisted_and_keeps_other_account(self):
        client = self.app.platform_client
        login_fixture(self.app, self.browser, 'second')
        with patch.object(client, 'request', side_effect=httpx.ConnectError('offline')):
            with self.assertRaises(RuntimeError):
                client.call(self.browser, 'account', 'POST', 'logout', {}, '', self.store.enrollment()['id'])
        self.restart()
        client = self.app.platform_client
        self.assertIsNone(client.current(self.browser))
        self.assertEqual([key[2] for key in client.accounts], ['p_seller'])

    def test_idle_expiration_and_active_renewal(self):
        future = time.time() + 2 * 24 * 3600
        with patch('time.time', return_value=future):
            self.assertIsNotNone(self.local.session('tm_session=' + self.browser))
            with patch.object(self.app.platform_client, 'request', return_value=(200, self.view, {})):
                self.app.platform_client.snapshot(self.browser)
            self.assertGreater(self.session['expires'], future + LIFETIME - 10)
        with patch('time.time', return_value=future + LIFETIME + 1):
            self.restart_expired()
        with patch('time.time', return_value=future + LIFETIME + BROWSER_LIFETIME + 2):
            self.assertIsNone(LocalSessions(self.temp.name).session('tm_session=' + self.browser))

    def restart_expired(self):
        self.assertIsNotNone(LocalSessions(self.temp.name).session('tm_session=' + self.browser))
        self.app.platform_client = PlatformClient(self.app)
        session=self.app.platform_client.current(self.browser)
        self.assertTrue(session['restored'])
        self.assertFalse(self.app.platform_client.authorized(self.browser))

    def test_signed_out_private_identity_survives_and_password_apis_are_absent(self):
        client=self.app.platform_client
        iid=self.session['account']['identity_id']
        key=client.identities.load(client.base(self.browser),iid)
        with patch.object(client,'request',return_value=(200,{'ok':True},{})):
            client.call(self.browser,'account','POST','logout',{},'',self.store.enrollment()['id'])
        self.restart()
        client=self.app.platform_client
        self.assertIsNone(client.current(self.browser))
        self.assertEqual(client.identities.load(client.base(self.browser),iid)['seed'],key['seed'])
        self.assertNotIn(key['seed'].encode(),client.identities.file(iid).path.read_bytes())
        for path in ('password','saved-password'):
            with self.assertRaises(ValueError):
                client.call(self.browser,'account','POST',path,{},'',self.store.enrollment()['id'])

    def test_more_than_64_local_identities_survive_restart(self):
        client=self.app.platform_client
        base=client.base(self.browser)
        first=self.session['account']['identity_id']
        for _ in range(70):client.identities.create(base)
        self.restart()
        rows=self.app.platform_client.identities.list(base)
        self.assertEqual(len(rows),71)
        self.assertIn(first,[row['identity_id'] for row in rows])
        self.assertTrue(all('seed' not in row for row in rows))


if __name__ == '__main__':
    unittest.main()
