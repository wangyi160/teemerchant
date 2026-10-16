import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from store import Store
from transport import Transport


class PortableTests(unittest.TestCase):
    def test_policies_and_identity_survive_directory_move_and_external_file_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original, relocated = root / 'first' / 'data', root / 'moved' / 'data'
            original.mkdir(parents=True)
            policy = root / 'approved.json'
            raw = b'{"fixture":true}\r\n'
            policy.write_bytes(raw)
            store = Store(original)
            identity = store.enrollment()
            store.save_connection({'router':'https://router.example', 'transport':'builtin',
                                   'tdx_policy':str(policy), 'snp_policy':''})
            relative = store.settings['tdx_policy']
            self.assertFalse(Path(relative).is_absolute())
            self.assertEqual(store.policy_path(relative).read_bytes(), raw)
            store.archive('usage', 'fixture', [{'seq':1,'event':{'user_id':'fixture'}}])
            store.db.close()
            policy.unlink()
            self.assertEqual(original.parent.parent, root)
            self.assertEqual(relocated.parent.parent, root)
            original.parent.rename(relocated.parent)
            store = Store(relocated)
            try:
                self.assertEqual(store.enrollment(), identity)
                self.assertEqual(len(store.records('usage')), 1)
                self.assertEqual(store.policy_path(relative).read_bytes(), raw)
                # Transport resolves stored relative paths against data, never CWD.
                with patch('transport.subprocess.Popen'), patch.object(Transport, 'read', return_value={'ready':True,'scope':'merchant'}):
                    transport = Transport(store, bundled_transport=__file__)
                    transport.start()
                config = (relocated / 'transport.toml').read_text(encoding='utf-8')
                self.assertIn(json.dumps(str((relocated / relative).resolve())), config)
                self.assertNotIn(str(original), config)
            finally:
                store.db.close()

    def test_invalid_policy_or_failed_settings_write_keeps_old_trust_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / 'policy.json'
            policy.write_text('{"version":1}')
            store = Store(root / 'data')
            body = {'router':'https://router.example','transport':'builtin','tdx_policy':str(policy),'snp_policy':''}
            store.save_connection(body)
            previous = store.settings.copy()
            old_bytes = store.policy_path(previous['tdx_policy']).read_bytes()
            try:
                policy.write_text('invalid')
                with self.assertRaises(ValueError): store.save_connection(body)
                self.assertEqual(store.settings, previous)
                policy.write_text('{"version":2}')
                with patch.object(store, 'save', side_effect=OSError('full')):
                    with self.assertRaises(OSError): store.save_connection(body)
                self.assertEqual(store.settings, previous)
                self.assertEqual(store.policy_path(previous['tdx_policy']).read_bytes(), old_bytes)
                reopened = Store(root / 'data')
                try: self.assertEqual(reopened.settings, previous)
                finally: reopened.db.close()
            finally:
                store.db.close()


if __name__ == '__main__': unittest.main()
