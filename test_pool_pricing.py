"""Price edits commit on Router confirmation, never on local staging."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from app import Application
from local_accounts import config_from
from store import Store
from test_browser import FakeRouter


class PoolPricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.settings['router'] = 'https://fixture.example'
        self.router = FakeRouter(self.store)
        self.app = Application(self.store, self.router)
        self.app.accept(copy.deepcopy(self.router.remote), clean_catalog=False)
        self.book = self.store.accounts
        self.url = self.store.settings['router']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def body(self, pool='openai', multiplier=50000):
        view = self.book.view(self.url)
        return {'pool': pool, 'multiplier_millionths': multiplier,
                'expected_revision': view['settings']['revision'], '_local_revision': view['local_revision']}

    def save(self, body=None):
        return self.app.api('PUT', '/api/pool-pricing', body or self.body())

    def rate(self, view=None):
        return next(p['multiplier_millionths'] for p in (view or self.book.view(self.url))['settings']['pool_pricing'] if p['pool'] == 'openai')

    def test_success_only_changes_price_and_never_stages_before_acknowledgement(self):
        previous = config_from(self.router.remote)
        call = self.router.call
        def inspect(method, path, body=None):
            if method == 'PUT':
                self.assertEqual(self.rate(), 200000)
                self.assertIsNone(self.book.pending(self.url))
                other = Store(self.tmp.name)
                try:
                    self.assertEqual(self.rate(other.accounts.view(self.url)), 200000)
                finally:
                    other.db.close()
                self.assertEqual({k for k in previous if previous[k] != body[k]}, {'pool_pricing'})
                self.assertNotIn('accounts', body)
                self.assertNotIn('secrets', body)
            return call(method, path, body)
        self.router.call = inspect
        with patch.object(self.app, 'renew_credentials', side_effect=AssertionError('must not renew')):
            result = self.save()
        self.assertEqual(self.rate(result), 50000)
        self.assertEqual(self.rate(self.router.remote), 50000)
        self.assertEqual(result['local_sync']['state'], 'synced')
        self.assertIsNone(self.book.pending(self.url))

    def test_router_rejection_keeps_old_price_on_disk_and_no_pending_write(self):
        self.router.reject = True
        with self.assertRaisesRegex(RuntimeError, '倍率保存失败'):
            self.save()
        self.assertEqual(self.rate(), 200000)
        self.assertEqual(self.rate(self.router.remote), 200000)
        self.assertIsNone(self.book.pending(self.url))
        other = Store(self.tmp.name)
        try:
            self.assertEqual(self.rate(other.accounts.view(self.url)), 200000)
            self.assertIsNone(other.accounts.pending(self.url))
        finally:
            other.db.close()
        self.app.push_config()
        self.assertEqual(self.router.writes, [])

    def test_connection_failure_does_not_stage_anything(self):
        with patch.object(self.router, 'call', side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError, '倍率未修改'):
                self.save()
        self.assertEqual(self.rate(), 200000)
        self.assertIsNone(self.book.pending(self.url))

    def test_offline_does_not_stage_or_connect(self):
        self.app.connected = False
        with patch.object(self.router, 'call', side_effect=AssertionError('no request')):
            with self.assertRaisesRegex(ValueError, '倍率未修改'):
                self.save()
        self.assertIsNone(self.book.pending(self.url))

    def test_lost_acknowledgement_is_confirmed_by_read_without_replay(self):
        call = self.router.call
        def lost(method, path, body=None):
            result = call(method, path, body)
            if method == 'PUT':
                raise TimeoutError()
            return result
        self.router.call = lost
        self.assertEqual(self.rate(self.save()), 50000)
        self.assertEqual(len(self.router.writes), 1)
        self.assertIsNone(self.book.pending(self.url))

    def test_unknown_outcome_keeps_last_confirmed_price_then_reconciles_read_only(self):
        call = self.router.call
        def lost(method, path, body=None):
            if self.router.writes:
                raise TimeoutError()
            result = call(method, path, body)
            if method == 'PUT':
                raise TimeoutError()
            return result
        self.router.call = lost
        with self.assertRaisesRegex(RuntimeError, '未能确认倍率修改结果'):
            self.save()
        self.assertEqual(self.rate(), 200000)
        self.assertIsNone(self.book.pending(self.url))
        self.app.accept(call('GET', '/merchant/state'), clean_catalog=False)
        self.assertEqual(self.rate(), 50000)
        self.assertEqual(len(self.router.writes), 1)

    def test_stale_form_does_not_overwrite_remote_price(self):
        self.router.remote['settings']['revision'] += 1
        self.router.remote['settings']['pool_pricing'][0]['multiplier_millionths'] = 300000
        with self.assertRaisesRegex(ValueError, '配置已变化'):
            self.save()
        self.assertEqual(self.rate(), 300000)
        self.assertEqual(self.router.writes, [])

    def test_other_pending_account_edits_and_secrets_are_not_sent_or_discarded(self):
        pending = config_from(self.app.remote)
        pending['pools'][0]['deployments'][0]['rpm'] = 11
        pending['accounts'] = {'credential-ref': {'credential': {'access_token': 'PRIVATE_FIXTURE_ACCESS', 'refresh_token': 'PRIVATE_FIXTURE_REFRESH'}}}
        self.book.stage(self.url, pending)
        before = copy.deepcopy(self.book.data['routers'][self.url]['accounts'])
        self.save()
        sent = self.router.writes[-1]
        self.assertEqual(sent['pools'][0]['deployments'][0]['rpm'], 30)
        self.assertNotIn('accounts', sent)
        self.assertEqual(self.book.pending(self.url)['pools'][0]['deployments'][0]['rpm'], 11)
        self.assertEqual(self.book.pending(self.url)['expected_revision'], self.router.remote['settings']['revision'])
        self.assertEqual(self.book.data['routers'][self.url]['accounts'], before)
        self.assertEqual(self.rate(), 50000)

    def test_legacy_config_endpoint_rejects_published_rate_change(self):
        body = config_from(self.app.remote)
        body['pool_pricing'][0]['multiplier_millionths'] = 50000
        with self.assertRaisesRegex(ValueError, '倍率未修改'):
            self.app.api('PUT', '/api/config', body)
        self.assertEqual(self.rate(), 200000)
        self.assertIsNone(self.book.pending(self.url))

    def test_old_pending_rate_is_removed_when_router_is_observed(self):
        body = config_from(self.app.remote)
        body['pool_pricing'][0]['multiplier_millionths'] = 50000
        self.book.stage(self.url, body)  # Simulate a draft saved by the old version.
        self.app.accept(copy.deepcopy(self.router.remote), clean_catalog=False)
        self.assertEqual(self.rate(), 200000)
        self.assertEqual(self.book.pending(self.url)['pool_pricing'], self.router.remote['settings']['pool_pricing'])

    def test_invalid_rate_does_not_write(self):
        for value in (-1, 10**15+1, True, 0.5, '0.5'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '倍率参数无效'):
                self.save(self.body(multiplier=value))
        self.assertEqual(self.router.writes, [])


if __name__ == '__main__':
    unittest.main()
