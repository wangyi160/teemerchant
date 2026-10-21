import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from app import Application
from store import Store
from test_local_accounts import LocalAccountTests, Router


class OwnerControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.settings['router'] = 'https://fixture.example'
        self.store.save()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_owner_key_is_local_encrypted_persistent_and_hidden(self):
        token = self.store.control_token()
        owner = token.split('.')[1]
        self.assertNotEqual(owner, self.store.settings['token'])
        self.assertNotIn(owner, json.dumps(self.store.public()))
        self.assertNotIn(owner, json.dumps(self.store.credentials(reveal=True)))
        self.assertNotIn(owner.encode(), self.store.path.read_bytes())
        other = Store(self.tmp.name)
        try:
            self.assertEqual(other.control_token(), token)
        finally:
            other.db.close()

    def test_local_write_failure_prevents_owner_binding(self):
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.control_token()
        self.assertNotIn('owner_secret', self.store.settings)

    def test_rotated_credentials_are_saved_locally_without_overwriting_pending_edits(self):
        book = self.store.accounts
        router = self.store.settings['router']
        current = book._book(book.data, router)
        current['config'] = {'expected_revision': 5, 'pools': [{'deployments': [
            {'id': 'a', 'provider': 'xai', 'oauth_account': 'a'}]}]}
        current['accounts'] = {'a': {'provider': 'xai', 'credential': {'access_token': 'OLD'}}}
        snapshot = {'merchant_id': self.store.enrollment()['id'], 'revision': 5,
                    'accounts': {'a': {'provider': 'xai', 'credential': {'access_token': 'NEW', 'refresh_token': 'ROTATED'}}}}
        self.assertTrue(book.sync_credentials(router, snapshot))
        self.assertNotIn(b'ROTATED', book.path.read_bytes())
        self.assertEqual(book.data['routers'][router]['accounts']['a']['credential']['refresh_token'], 'ROTATED')
        book.data['routers'][router]['pending'] = {'accounts': {'a': {'credential': {'access_token': 'EDIT'}}}}
        snapshot['accounts']['a']['credential']['access_token'] = 'NEWER'
        self.assertFalse(book.sync_credentials(router, snapshot))
        self.assertEqual(book.data['routers'][router]['accounts']['a']['credential']['access_token'], 'NEW')
        snapshot['merchant_id'] = 'm_' + 'b' * 32
        with self.assertRaises(RuntimeError):
            book.sync_credentials(router, snapshot)

    def test_restart_restores_local_accounts_and_requires_owner_confirmation(self):
        router = Router(self.store)
        app = Application(self.store, router)
        app.accept(router.remote)
        fixture = type('Fixture', (), {'app': app, 'router': router})()
        config = LocalAccountTests.config(fixture)
        app.api('PUT', '/api/config', config)
        original = router.call
        router.remote['pools'] = []
        router.remote['merchant'].update(configuration_required=True,
            owner_hash=hashlib.sha256(self.store.control_token().split('.')[1].encode()).hexdigest())
        def request(method, path, body=None):
            if path == '/merchant/bind':
                return {'merchant_id': self.store.enrollment()['id'], 'owner_hash': router.remote['merchant']['owner_hash'],
                        'merchant_control_version': 1}
            result = original(method, path, body)
            if method == 'PUT':
                router.remote['merchant']['configuration_required'] = False
                result['merchant']['configuration_required'] = False
            return result
        router.call = request
        app.connect()
        self.assertEqual(len(router.writes), 2)
        self.assertEqual(router.writes[-1]['secrets']['api-1'], 'PRIVATE_TEST_KEY')
        self.assertEqual(len(app.remote['pools']), 1)
        router.remote['merchant']['owner_hash'] = 'b' * 64
        with self.assertRaises(RuntimeError):
            app.connect()
        self.assertEqual(len(router.writes), 2)

    def test_offline_edits_preserve_private_protocol_configuration_for_restart(self):
        router = Router(self.store)
        app = Application(self.store, router)
        app.accept(router.remote)
        fixture = type('Fixture', (), {'app': app, 'router': router})()
        app.api('PUT', '/api/config', LocalAccountTests.config(fixture))
        book = self.store.accounts.data['routers'][self.store.settings['router']]
        book['config']['compatibility'] = {'fixture': ['preserve']}
        book['config']['restrictions'] = [{'fixture': 'preserve'}]
        app.connected = False
        app.api('PUT', '/api/config', LocalAccountTests.config(fixture))
        pending = self.store.accounts.pending(self.store.settings['router'])
        self.assertEqual(pending['compatibility'], {'fixture': ['preserve']})
        self.assertEqual(pending['restrictions'], [{'fixture': 'preserve'}])


if __name__ == '__main__':
    unittest.main()
