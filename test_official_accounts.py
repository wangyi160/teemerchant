import copy
import json
from pathlib import Path
import tempfile
import unittest
from official_accounts import retire_unsupported
from local_accounts import AccountBook


class OfficialAccountsTests(unittest.TestCase):
    def fixture(self):
        pools = [
            {'name': 'real', 'provider': 'xai', 'deployments': [{'id': 'real-account', 'provider': 'xai', 'oauth_account': 'real', 'api_key_ref': 'shared'}]},
            {'name': 'test', 'provider': 'workbuddy', 'deployments': [{'id': 'test-account', 'provider': 'workbuddy', 'oauth_account': 'retired', 'api_key_ref': 'retired-key'}, {'id': 'test2', 'provider': 'workbuddy', 'api_key_ref': 'shared'}]},
        ]
        config = {'pools': pools, 'pool_pricing': [{'pool': p['name']} for p in pools]}
        return {'version': 1, 'identity': 'merchant', 'routers': {'https://router.test': {
            'config': copy.deepcopy(config), 'pending': copy.deepcopy(config),
            'remote': {**copy.deepcopy(config), 'merchant': {'allow_test': True}, 'accounts': {'data': [{'name': 'real', 'provider': 'xai'}, {'name': 'retired', 'provider': 'workbuddy'}]},
                       'catalog': {'data': [{'pool': 'real'}, {'pool': 'test'}]}, 'status': {'models': [{'model': 'real'}, {'model': 'test'}]}},
            'accounts': {'real': {'provider': 'xai', 'credential': {'access_token': 'KEEP', 'refresh_token': 'KEEP_REFRESH'}}, 'retired': {'provider': 'workbuddy', 'credential': {'api_key': 'REMOVE'}}},
            'secrets': {'retired-key': 'REMOVE', 'shared': 'KEEP_SHARED'},
            'account_sync': {'test-account': {'state': 'synced'}, 'real-account': {'state': 'synced'}},
            'token_sync': {'retired': {}, 'real': {'last_success_at': 1}},
            'local_revision': 7}}}

    def test_removes_all_active_references_without_changing_real_credentials(self):
        before = self.fixture()
        after = retire_unsupported(before)
        book = after['routers']['https://router.test']
        for field in ('config', 'pending', 'remote'):
            self.assertEqual([p['name'] for p in book[field]['pools']], ['real'])
            self.assertEqual(book[field]['pool_pricing'], [{'pool': 'real'}])
        self.assertEqual(book['accounts'], {'real': before['routers']['https://router.test']['accounts']['real']})
        self.assertEqual(book['secrets'], {'shared': 'KEEP_SHARED'})
        self.assertEqual(book['account_sync'], {'real-account': {'state': 'synced'}})
        self.assertEqual(book['token_sync'], {'real': {'last_success_at': 1}})
        self.assertNotIn('workbuddy', json.dumps(after))
        self.assertEqual(retire_unsupported(after), after)
        self.assertEqual(before, self.fixture())

    def test_migration_retains_original_sealed_backup_and_reopens_idempotently(self):
        def protect(data, decrypt=False):
            return data[7:][::-1] if decrypt else b'sealed:' + data[::-1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = protect(json.dumps(self.fixture()).encode())
            (root / 'accounts.dpapi').write_bytes(original)
            book = AccountBook(root, 'merchant', protect)
            self.assertEqual((root / 'accounts-before-official-only.dpapi').read_bytes(), original)
            self.assertEqual(book.data, retire_unsupported(self.fixture()))
            sealed = book.path.read_bytes()
            again = AccountBook(root, 'merchant', protect)
            self.assertEqual(again.path.read_bytes(), sealed)
            self.assertEqual(again.data, book.data)
