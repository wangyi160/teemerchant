import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import Application
from store import Store
from test_app import Transport
import test_local_accounts
from test_revenue import receipt


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

    def test_import_persists_encrypted_and_state_never_returns_secret(self):
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

    def test_explicit_local_view_retrieves_credentials_without_router_request(self):
        data = issued()
        self.store.import_credentials(data)
        with patch.object(self.app.transport, 'call', side_effect=AssertionError('Must stay local')):
            metadata = self.app.api('GET', '/api/state', {})['credentials']
            self.assertEqual(metadata, {'saved':True, 'merchant_id':data['merchant_id']})
            self.assertEqual(self.app.api('GET', '/api/credentials', {}), {'saved':True, **data})

    def test_fresh_profile_has_no_merchant_credentials_and_cannot_connect(self):
        self.assertEqual(self.app.api('GET', '/api/credentials', {}),
                         {'saved':False,'merchant_id':'','merchant_secret':''})
        with patch.object(self.app.transport, 'call', side_effect=AssertionError('Must not send placeholder token')):
            with self.assertRaises(ValueError): self.app.api('POST', '/api/connect', {})

    def test_legacy_saved_credentials_remain_available(self):
        self.store.settings.pop('credentials_saved')
        self.store.save()
        reopened = Store(self.tmp.name)
        try:
            self.assertTrue(reopened.credentials()['saved'])
            self.assertEqual(reopened.credentials(reveal=True)['merchant_secret'], self.store.settings['token'])
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

    def test_switch_isolates_vault_pending_archives_revision_and_restores_both(self):
        first, second = issued(), issued()
        self.store.import_credentials(first)
        policy = Path(self.tmp.name) / 'fixture.json'
        policy.write_text('{"fixture": true}', encoding='utf-8')
        self.store.save_connection({'router':'https://fixture.example', 'tdx_policy':str(policy), 'snp_policy':''})
        self.router = test_local_accounts.Router(self.store)
        self.app.accept(self.router.remote)
        self.app.connected = False
        self.app.api('PUT', '/api/config', test_local_accounts.LocalAccountTests.config(self))
        self.store.archive('usage','test',[receipt()])
        self.store.archive('refusals','test',[{'id':1,'text':'fixture refusal'}])
        self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'50')
        original_files = {name:(Path(self.tmp.name)/name).read_bytes() for name in ('settings.dpapi','accounts.dpapi')}
        pending = self.store.accounts.pending('https://fixture.example')
        self.app.last_error = 'old identity sync failure'
        with patch.object(self.app.transport, 'call', side_effect=AssertionError('Switch must stay local')):
            self.app.api('POST','/api/credentials',second)
            self.assertFalse(self.app.connected)
            self.assertEqual(self.app.last_error,'')
            self.assertIsNone(self.app.remote)
            self.assertEqual(self.store.settings['minimum_revision'],0)
            self.assertEqual(self.store.settings['router'],'https://fixture.example')
            self.assertEqual(self.store.policy_path(self.store.settings['tdx_policy']).read_bytes(),policy.read_bytes())
            self.assertEqual(self.store.records('usage'),[])
            self.assertEqual(self.store.records('refusals'),[])
            self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'0')
            with self.assertRaises(ValueError): self.app.api('GET','/api/accounts/alias-1',{})
            with self.assertRaises(ValueError): self.store.revenue_record('test',1)
            self.store.archive('usage','test',[receipt('second',multiplier=400000)])
            self.store.settings['minimum_revision']=7
            self.store.save()
            self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'100')
            reopened = Store(self.tmp.name)
            try:
                self.assertEqual(reopened.enrollment()['id'],second['merchant_id'])
                self.assertEqual(reopened.revenue()['summary']['charge_microusd'],'100')
            finally: reopened.db.close()
            self.app.api('POST','/api/merchant-profile',{'merchant_id':first['merchant_id']})
            self.assertEqual(self.store.credentials(reveal=True)['merchant_secret'],first['merchant_secret'])
            self.assertEqual(self.store.settings['minimum_revision'],1)
            self.assertEqual(self.store.accounts.pending('https://fixture.example'),pending)
            self.assertEqual(self.app.api('GET','/api/accounts/alias-1',{})['credential_text'],'PRIVATE_TEST_KEY')
            self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'50')
            self.assertEqual(len(self.store.records('refusals')),1)
            for name, raw in original_files.items(): self.assertEqual((Path(self.tmp.name)/name).read_bytes(),raw)
            self.store.import_credentials(first)
            self.assertEqual(self.store.accounts.pending('https://fixture.example'),pending)
            # Entering a saved pair switches back without overwriting its ledger.
            self.app.api('POST','/api/credentials',second)
            self.assertEqual(self.store.settings['minimum_revision'],7)
            self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'100')
        state=json.dumps(self.app.api('GET','/api/state',{}))
        self.assertNotIn(first['merchant_secret'],state)
        self.assertNotIn(second['merchant_secret'],state)
        self.assertEqual(len(self.store.profiles()),2)

    def test_failed_switch_and_unknown_identity_keep_active_profile(self):
        first,second=issued(),issued()
        self.store.import_credentials(first)
        self.store.import_credentials(second)
        pointer=(Path(self.tmp.name)/'active-merchant.json').read_bytes()
        with patch('store.os.replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.switch_profile({'merchant_id':first['merchant_id']})
        for identity in (issued()['merchant_id'],'../settings.dpapi'):
            with self.assertRaises(ValueError): self.store.switch_profile({'merchant_id':identity})
        self.assertEqual(self.store.enrollment()['id'],second['merchant_id'])
        self.assertEqual((Path(self.tmp.name)/'active-merchant.json').read_bytes(),pointer)
        reopened=Store(self.tmp.name)
        try: self.assertEqual(reopened.enrollment()['id'],second['merchant_id'])
        finally: reopened.db.close()

    def test_new_profile_failure_leaves_no_partial_identity(self):
        first,second=issued(),issued()
        self.store.import_credentials(first)
        with patch.object(Store,'save_connection',side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.import_credentials(second)
        self.assertEqual(self.store.enrollment()['id'],first['merchant_id'])
        self.assertEqual(len(self.store.profiles()),1)
        self.assertFalse((Path(self.tmp.name)/'active-merchant.json').exists())
        self.store.import_credentials(second)
        self.assertEqual(len(self.store.profiles()),2)

    def test_previous_identity_requests_are_rejected_before_any_read_or_write(self):
        first,second=issued(),issued()
        self.store.import_credentials(first)
        self.app.api('POST','/api/credentials',second,first['merchant_id'])
        with patch.object(self.app.transport,'call',side_effect=AssertionError('Must not contact Router')):
            for method,path in [('PUT','/api/config'),('POST','/api/sync'),('POST','/api/connect'),
                                ('GET','/api/credentials'),('GET','/api/records/usage'),('POST','/api/merchant-profile')]:
                with self.assertRaisesRegex(ValueError,'已切换'):
                    self.app.api(method,path,{},first['merchant_id'])
        self.assertIsNone(self.app.remote)
        self.assertEqual(self.app.api('GET','/api/credentials',{},second['merchant_id'])['merchant_id'],second['merchant_id'])

    def test_missing_or_mismatched_selected_profile_never_falls_back(self):
        first,second=issued(),issued()
        self.store.import_credentials(first)
        self.store.import_credentials(second)
        selected=self.store.path
        raw=selected.read_bytes()
        selected.unlink()
        with self.assertRaises(ValueError): Store(self.tmp.name)
        selected.write_bytes((Path(self.tmp.name)/'settings.dpapi').read_bytes())
        with self.assertRaises(ValueError): Store(self.tmp.name)
        selected.write_bytes(raw)
        self.assertEqual(self.store.enrollment()['id'],second['merchant_id'])

    def test_failed_atomic_save_keeps_old_identity_in_memory_and_on_disk(self):
        original = self.store.enrollment()
        with patch('store.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.import_credentials(issued())
        self.assertEqual(self.store.enrollment(), original)
        reopened = Store(self.tmp.name)
        try: self.assertEqual(reopened.enrollment(), original)
        finally: reopened.db.close()


if __name__ == '__main__': unittest.main()
