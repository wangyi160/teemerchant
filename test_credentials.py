import hashlib
import json
import secrets
import tempfile
import unittest
from unittest.mock import patch

from app import Application
from store import Store
from test_app import Transport


def issued():
    secret = secrets.token_hex(32)
    return {'merchant_id': 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32], 'merchant_secret': secret}


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.app = Application(self.store, Transport())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_import_persists_encrypted_and_never_returns_secret(self):
        data = issued()
        result = self.app.api('POST', '/api/credentials', data)
        self.assertEqual(result['merchant_id'], data['merchant_id'])
        self.assertFalse(self.app.connected)
        self.assertNotIn(data['merchant_secret'], json.dumps(self.app.api('GET', '/api/state', {})))
        self.assertNotIn(data['merchant_secret'].encode(), self.store.path.read_bytes())
        reopened = Store(self.tmp.name)
        try:
            self.assertEqual(reopened.settings['token'], data['merchant_secret'])
            self.assertEqual(reopened.accounts.identity, data['merchant_id'])
        finally:
            reopened.db.close()

    def test_bad_pair_and_old_password_endpoint_cannot_change_identity(self):
        original = self.store.enrollment()
        data = issued()
        data['merchant_id'] = issued()['merchant_id']
        with self.assertRaises(ValueError): self.app.api('POST', '/api/credentials', data)
        with self.assertRaises(ValueError): self.app.api('POST', '/api/credentials', {'username':'seller', 'password':'password'})
        with self.assertRaises(ValueError): self.app.api('POST', '/api/portal/bind', {})
        self.assertEqual(self.store.enrollment(), original)

    def test_existing_account_book_and_archives_prevent_identity_replacement(self):
        data = issued()
        self.store.import_credentials(data)
        self.app.accept({'merchant':{'id':data['merchant_id']}, 'settings':{'revision':1}})
        vault = self.store.accounts.path.read_bytes()
        self.store.archive('usage','test',[{'seq':1, 'event':{'user_id':'fixture'}}])
        with self.assertRaises(ValueError): self.store.import_credentials(issued())
        self.store.import_credentials(data)  # Reentering the same credentials is safe.
        self.assertEqual(self.store.accounts.path.read_bytes(), vault)
        self.assertEqual(self.store.enrollment()['id'], data['merchant_id'])
        self.assertEqual(self.store.settings['minimum_revision'], 1)
        self.assertEqual(len(self.store.records('usage')), 1)

    def test_failed_atomic_save_keeps_old_identity_in_memory_and_on_disk(self):
        original = self.store.enrollment()
        with patch('store.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.import_credentials(issued())
        self.assertEqual(self.store.enrollment(), original)
        reopened = Store(self.tmp.name)
        try: self.assertEqual(reopened.enrollment(), original)
        finally: reopened.db.close()


if __name__ == '__main__': unittest.main()
