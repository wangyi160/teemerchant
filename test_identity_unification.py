import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest

from identity_unification import migrate, remap
from key_identities import Identities
from store import Store, protector


class IdentityMigrationTests(unittest.TestCase):
    def test_two_profiles_preserve_private_keys_tokens_and_signed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            keys = Identities(root)
            expected = []
            for index in range(2):
                local = keys.create('https://teerouter.org', 'p_old_' + str(index))
                secret = secrets.token_hex(32)
                store.import_credentials({'merchant_id': local['identity_id'], 'merchant_secret': secret})
                owner = store.control_token()
                old = 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32]
                store.settings['platform_identity'] = {'platform_url': 'https://teerouter.org',
                    'account_id': 'p_old_' + str(index), 'identity_id': local['identity_id'],
                    'username': local['identity_id'], 'name': 'Fixture', 'legacy': False}
                store.accounts.data['routers'] = {'https://router.teerouter.org': {
                    'accounts': {'a': {'credential': {'access_token': 'TOKEN', 'refresh_token': 'REFRESH'}}},
                    'recovery_customers': [{'user_id': 'u_old', 'enabled': False}]}}
                store.accounts.data['identity'] = old
                store.accounts._write(store.accounts.data)
                store.settings.pop('merchant_id')
                store.save()
                store.archive('usage', 'old-boot', [{'seq': 1, 'event': {'merchant_id': old}}])
                expected.append((old, local, owner, store.directory))
                # Restore the in-memory identity before selecting a different vault.
                store.settings['merchant_id'] = local['identity_id']
            store.db.close()
            # Simulate the old portable directory and pointer layout.
            path = expected[1][3]
            oldpath = path.with_name(expected[1][0])
            path.rename(oldpath)
            (root / 'active-merchant.json').write_text(json.dumps({'merchant_id': expected[1][0]}))
            migrate(root, protector, {'u_old': 'u_sig_' + '1' * 64})
            reopened = Store(root)
            try:
                self.assertEqual(reopened.enrollment()['id'], expected[1][1]['identity_id'])
                self.assertEqual(len(reopened.profiles()), 2)
                for old, local, owner, _ in expected:
                    reopened.switch_profile({'merchant_id': local['identity_id']})
                    self.assertEqual(reopened.control_token(), owner)
                    self.assertEqual(reopened.settings['platform_identity']['account_id'], local['identity_id'])
                    self.assertEqual(keys.load('https://teerouter.org', local['identity_id'])['seed'], local['seed'])
                    book = reopened.accounts.data['routers']['https://router.teerouter.org']
                    self.assertEqual(book['accounts']['a']['credential']['refresh_token'], 'REFRESH')
                    self.assertEqual(book['recovery_customers'], [{'user_id': 'u_sig_' + '1' * 64, 'enabled': False}])
                    self.assertEqual(reopened.records('usage')[0]['event']['merchant_id'], old)
                self.assertTrue((root / 'identity-migration-backup/settings.dpapi').is_file())
            finally:
                reopened.db.close()

    def test_reference_collisions_fail_instead_of_merging_customer_policies(self):
        with self.assertRaisesRegex(ValueError, 'collision'):
            remap({'old': {'enabled': False}, 'new': {'enabled': True}}, {'old': 'new'})


if __name__ == '__main__':
    unittest.main()
