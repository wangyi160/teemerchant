import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from app import Application
from store import Store


class Router:
    def __init__(self, store):
        self.store, self.writes, self.failure = store, [], False
        self.remote = {'merchant': {'id': store.enrollment()['id'], 'name': 'Fixture', 'allow_test': False},
            'settings': {'revision': 1, 'max_concurrent': 16, 'rpm': 0, 'tpm': 0,
                         'customer_defaults': {'enabled': True, 'models': [], 'max_concurrent': 4, 'rpm': 0, 'tpm': 0},
                         'customers': [], 'pool_pricing': []}, 'pools': [], 'accounts': {'data': []}, 'catalog': {'data': []}}

    def call(self, method, path, body=None):
        if method == 'PUT':
            # The durable account file must exist before the transport sees any key.
            assert self.store.accounts.path.exists()
            assert self.store.accounts.pending('https://fixture.example')['secrets'].get('api-1') == 'PRIVATE_TEST_KEY'
            if self.failure:
                raise RuntimeError('Router 返回 409：revision_conflict')
            if body['expected_revision'] != self.remote['settings']['revision']:
                raise RuntimeError('Router 返回 409：revision_conflict')
            self.writes.append(copy.deepcopy(body))
            self.remote['pools'] = copy.deepcopy(body['pools'])
            for pool in self.remote['pools']:
                for account in pool['deployments']:
                    if account.get('proxy'):
                        account['proxy'] = '[redacted]'
            self.remote['settings'].update({k: copy.deepcopy(body[k]) for k in ('pool_pricing', 'max_concurrent', 'rpm', 'tpm')})
            self.remote['settings']['revision'] += 1
        return copy.deepcopy(self.remote)

    def close(self):
        pass


class LocalAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.settings['router'] = 'https://fixture.example'
        self.store.save()
        self.router = Router(self.store)
        self.app = Application(self.store, self.router)
        self.app.accept(self.router.remote)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def config(self):
        return {'expected_revision': self.app.remote['settings']['revision'],
                '_local_revision': self.app.remote['local_revision'],
                'pools': [{'name': 'openai', 'provider': 'openai', 'deployments': [
                    {'id': 'alias-1', 'provider': 'openai', 'api_key_ref': 'api-1',
                     'proxy': 'socks5h://user:PRIVATE_PROXY_PASSWORD@fixture.example:1080',
                     'enabled': True, 'max_concurrent': 16, 'rpm': 0}]}],
                'customers': [], 'customer_defaults': self.router.remote['settings']['customer_defaults'],
                'pool_pricing': [{'pool': 'openai', 'multiplier_millionths': 1000000}],
                'max_concurrent': 16, 'rpm': 0, 'tpm': 0, 'secrets': {'api-1': 'PRIVATE_TEST_KEY'}}

    def test_pool_model_policy_is_durable_and_invalid_lists_are_not_saved(self):
        for invalid in [['*'], ['x', 'x'], ['a/../b'], [42], 'all', ['x'] * 4097]:
            body = self.config()
            body['pools'][0]['allowed_models'] = invalid
            with self.assertRaises(ValueError):
                self.app.api('PUT', '/api/config', body)
        self.assertEqual(self.router.writes, [])
        body = self.config()
        body['pools'][0]['allowed_models'] = ['model-a', 'model-b']
        self.app.api('PUT', '/api/config', body)
        self.assertEqual(self.router.writes[-1]['pools'][0]['allowed_models'], ['model-a', 'model-b'])
        other = Store(self.tmp.name)
        try:
            view = other.accounts.view('https://fixture.example')
            self.assertEqual(view['pools'][0]['allowed_models'], ['model-a', 'model-b'])
        finally:
            other.db.close()

    def test_local_write_precedes_remote_and_is_readable_after_restart(self):
        self.app.api('PUT', '/api/config', self.config())
        self.assertEqual(len(self.router.writes), 1)
        self.assertNotIn('_local_revision', self.router.writes[0])
        raw = self.store.accounts.path.read_bytes()
        self.assertNotIn(b'PRIVATE_TEST_KEY', raw)
        self.assertNotIn(b'PRIVATE_PROXY_PASSWORD', raw)
        other = Store(self.tmp.name)
        try:
            offline = Application(other, self.router)
            self.assertFalse(offline.connected)
            account = offline.api('GET', '/api/accounts/alias-1', {})
            self.assertEqual(account['credential_text'], 'PRIVATE_TEST_KEY')
            self.assertIn('PRIVATE_PROXY_PASSWORD', account['proxy'])
            self.assertNotIn('PRIVATE_', json.dumps(offline.api('GET', '/api/state', {})))
        finally:
            other.db.close()

    def test_disk_failure_prevents_router_write(self):
        with patch.object(self.store.accounts, '_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.app.api('PUT', '/api/config', self.config())
        self.assertEqual(self.router.writes, [])
        self.assertIsNone(self.store.accounts.pending(self.store.settings['router']))

    def test_acknowledgement_failure_retains_durable_pending_request_without_blind_replay(self):
        with patch.object(self.store.accounts, 'acknowledge', side_effect=OSError('disk full')):
            result = self.app.api('PUT', '/api/config', self.config())
        self.assertEqual(len(self.router.writes), 1)
        self.assertEqual(result['local_sync']['state'], 'pending')
        self.assertIsNotNone(self.store.accounts.pending(self.store.settings['router']))
        self.app.api('POST', '/api/config/sync', {})
        self.assertEqual(len(self.router.writes), 1)
        self.assertIn('revision_conflict', self.app.remote['local_sync']['error'])

    def test_remote_failure_retains_local_material_pending_state_and_never_auto_replays(self):
        self.router.failure = True
        value = self.app.api('PUT', '/api/config', self.config())
        self.assertEqual(value['local_sync']['state'], 'pending')
        self.assertEqual(self.app.api('GET', '/api/accounts/alias-1', {})['credential_text'], 'PRIVATE_TEST_KEY')
        self.app.api('GET', '/api/merchant', {})
        self.assertEqual(len(self.app.remote['pools'][0]['deployments']), 1)
        self.assertEqual(self.router.writes, [])
        self.router.failure = False
        synced = self.app.api('POST', '/api/config/sync', {})
        self.assertEqual(synced['local_sync']['state'], 'synced')
        self.assertEqual(len(self.router.writes), 1)

    def test_offline_edits_are_saved_and_stale_local_forms_cannot_overwrite_them(self):
        self.app.connected = False
        body = self.config()
        self.app.api('PUT', '/api/config', body)
        self.assertEqual(self.router.writes, [])
        with self.assertRaisesRegex(ValueError, 'local_revision_conflict'):
            self.app.api('PUT', '/api/config', body)
        self.assertEqual(self.app.remote['local_sync']['state'], 'pending')

    def test_oauth_material_replacements_and_proxy_only_edits_do_not_merge_tokens(self):
        body = self.config()
        body['pools'][0]['deployments'][0].pop('api_key_ref')
        body['pools'][0]['deployments'][0].pop('proxy')
        body['pools'][0]['deployments'][0]['oauth_account'] = 'oauth-1'
        body['accounts'] = {'oauth-1': {'provider': 'openai', 'proxy': 'https://proxy.example',
            'credential': {'access_token': 'PRIVATE_ACCESS', 'refresh_token': 'PRIVATE_REFRESH', 'account_id': 'fixture'}}}
        self.app.connected = False
        self.app.api('PUT', '/api/config', body)
        replacement = copy.deepcopy(body)
        replacement['_local_revision'] = self.app.remote['local_revision']
        replacement['accounts']['oauth-1']['credential'] = {'access_token': 'NEW_ACCESS'}
        self.app.api('PUT', '/api/config', replacement)
        account = self.app.api('GET', '/api/accounts/alias-1', {})
        self.assertNotIn('PRIVATE_REFRESH', account['credential_text'])
        self.assertIn('NEW_ACCESS', account['credential_text'])
        replacement['_local_revision'] = self.app.remote['local_revision']
        replacement['accounts'] = {'oauth-1': {'proxy': 'https://second.example'}}
        self.app.api('PUT', '/api/config', replacement)
        self.assertIn('NEW_ACCESS', self.app.api('GET', '/api/accounts/alias-1', {})['credential_text'])

    def test_router_profiles_are_isolated(self):
        self.app.connected = False
        self.app.api('PUT', '/api/config', self.config())
        self.assertIsNone(self.store.accounts.view('https://different.example'))
        with self.assertRaises(ValueError):
            self.store.accounts.account('https://different.example', 'alias-1')

    def test_remote_changes_do_not_silently_replace_local_account_configuration(self):
        self.app.api('PUT', '/api/config', self.config())
        changed = copy.deepcopy(self.router.remote)
        changed['settings']['revision'] += 1
        changed['pools'][0]['deployments'][0]['max_concurrent'] = 1
        self.app.accept(changed)
        self.assertEqual(self.app.remote['pools'][0]['deployments'][0]['max_concurrent'], 16)
        self.assertEqual(self.app.remote['local_sync']['state'], 'remote_changed')

    def test_malformed_local_configuration_is_rejected_without_changing_the_account_book(self):
        for fields in ({'pools':[{}]}, {'secrets':[]}, {'accounts':{'bad-ref':{'credential':'not-an-object'}}},
                       {'max_concurrent':False}, {'delete_accounts':'bad'}, {'expected_revision':0}):
            before = self.store.accounts.path.read_bytes()
            with self.assertRaises(ValueError):
                self.app.api('PUT', '/api/config', {**self.config(), **fields})
            self.assertEqual(self.store.accounts.path.read_bytes(), before)
        self.assertEqual(self.router.writes, [])

    def test_recovery_imports_only_referenced_merchant_material(self):
        self.app.api('PUT', '/api/config', self.config())
        bundle = {'config': {'models': self.config()['pools']}, 'accounts': {},
                  'secrets': {'api-1': 'RECOVERED_KEY', 'merchant_internal_admin': 'DO_NOT_IMPORT', 'unrelated': 'DO_NOT_IMPORT'}}
        self.assertEqual(self.store.accounts.import_recovery(self.store.settings['router'], bundle), 1)
        self.assertEqual(self.store.accounts.account(self.store.settings['router'], 'alias-1')['credential_text'], 'RECOVERED_KEY')
        self.assertNotIn('DO_NOT_IMPORT', json.dumps(self.store.accounts.data))


if __name__ == '__main__':
    unittest.main()
