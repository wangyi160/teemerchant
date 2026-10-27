import json
import tempfile
import time
import unittest
from unittest.mock import patch
from store import Store
from transport import Transport


class RouterAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.settings.update(platform_url='https://platform.example', credentials_saved=True)
        self.transport = Transport(self.store)
        self.identity = self.store.enrollment()['id']

    def tearDown(self):
        self.transport.close()
        self.store.db.close()
        self.tmp.cleanup()

    def remember(self, seconds=30*86400):
        self.transport.remember_router_access({'token': 'scoped-login-admission', 'expires_at': int(time.time())+seconds},
                                             self.identity, 'https://platform.example')

    def test_admission_is_encrypted_per_identity_and_hidden_from_browser(self):
        self.remember()
        self.assertNotIn(b'scoped-login-admission', self.store.path.read_bytes())
        self.assertNotIn('router_access', self.store.public())
        reopened = Store(self.tmp.name)
        try:
            self.assertEqual(reopened.settings['router_access']['token'], 'scoped-login-admission')
        finally:
            reopened.db.close()
        with self.assertRaises(RuntimeError):
            self.transport.remember_router_access({'token': 'wrong', 'expires_at': int(time.time())+60},
                                                 'other-merchant', 'https://platform.example')

    def test_valid_token_requires_no_platform_request_and_outage_does_not_interrupt_it(self):
        self.remember()
        with patch('platform_data.PlatformData.fetch', side_effect=RuntimeError('offline')) as fetch:
            self.transport.ensure_router_access()
            fetch.assert_not_called()
        self.remember(3600)
        with patch('platform_data.PlatformData.fetch', side_effect=RuntimeError('offline')) as fetch:
            self.transport.ensure_router_access()
            self.transport.ensure_router_access()
            self.assertEqual(fetch.call_count, 1)

    def test_expired_missing_or_wrong_identity_token_never_falls_back_to_anonymous(self):
        for record in ({}, {'token': 'expired', 'expires_at': 1},
                       {'token': 'foreign', 'expires_at': int(time.time())+999999, 'merchant_id': 'other'}):
            self.store.settings['router_access'] = record
            with patch('platform_data.PlatformData.fetch', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    self.transport.ensure_router_access()

    def test_successful_renewal_closes_old_native_configuration(self):
        reply = {'merchant': {'id': self.identity}, 'router_access': {'token': 'renewed', 'expires_at': int(time.time())+30*86400}}
        with patch('platform_data.PlatformData.fetch', return_value=reply), patch.object(self.transport, 'close') as close:
            self.transport.ensure_router_access()
            close.assert_called_once()
        self.assertEqual(self.store.settings['router_access']['token'], 'renewed')

    def test_failed_local_persistence_preserves_previous_token(self):
        self.remember()
        previous = json.loads(json.dumps(self.store.settings))
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.transport.remember_router_access({'token': 'new-token', 'expires_at': int(time.time())+60},
                                                     self.identity, 'https://platform.example')
        self.assertEqual(previous, self.store.settings)
