import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import Application
from platform_history import PlatformHistory
from store import Store
from test_platform_client import login_fixture


def withdrawal(key=1, state='pending', version=1):
    return {'id': str(key), 'state': state, 'version': version, 'events': [],
            'amount_microusd': '1000000', 'fee_microusd': '10000', 'payout_microusd': '990000',
            'fee_rate_bps': 100, 'network': 'tron', 'network_name': 'TRON', 'asset': 'USDT',
            'address': 'fixture-address', 'created_at': 1792800000}


def withdrawals(rows=None):
    return {'data': rows if rows is not None else [withdrawal()], 'has_more': False, 'next_before': None,
            'balance': {k: '1000000' for k in ('available_microusd', 'reserved_microusd', 'paid_microusd',
                                             'paid_payout_microusd', 'paid_fee_microusd')},
            'can_apply': True, 'channels': [{'id': 'tron', 'name': 'TRON', 'asset': 'USDT'}],
            'fee_policy': {'version': 1, 'minimum_microusd': '1',
                           'tiers': [{'from_microusd': '1', 'to_microusd': None, 'rate_bps': 100}]}}


def receipt(seq=1):
    return {'seq': seq, 'batch_hash': 'a' * 64, 'model': 'fixture-model', 'provider': 'grok',
            'pool': 'test-pool', 'created_at': 1792800000, 'charge_microusd': '123'}


def proof(seq=1):
    return {'batch': {'batch_hash': 'a' * 64, 'manifest': json.dumps({
                'schema': 'teerouter.public-consumption-batch.v1', 'batch_no': 1, 'public_key': 'b' * 64,
                'records_sha256': 'c' * 64}), 'signature': 'd' * 128},
            'identity': {'evidence': 'fixture'}, 'selected_sequence': seq}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.settings.update(platform_url='https://platform.example', credentials_saved=True)
        self.store.save()
        self.app = Application(self.store)
        self.calls = []
        self.remote_withdrawals = [withdrawal()]
        self.remote_receipts = [receipt(2), receipt(1)]
        self.app.history.fetch = self.fetch

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def fetch(self, context, path):
        self.calls.append(path)
        if path == 'withdrawals':
            return withdrawals(copy.deepcopy(self.remote_withdrawals))
        if path.startswith('withdrawals/'):
            return copy.deepcopy(next(r for r in self.remote_withdrawals if r['id'] == path.split('/')[-1]))
        if path == 'receipts/mine':
            return {'data': copy.deepcopy(self.remote_receipts), 'has_more': False}
        if path.startswith('receipts/records/'):
            return proof(int(path.split('/')[-1]))
        self.fail(path)

    def test_automatic_mirror_survives_restart_and_proofs_are_deduplicated(self):
        with patch.object(self.app.transport, 'call', side_effect=AssertionError('No Router calls')):
            self.assertTrue(self.app.history.sync()['saved'])
        self.assertEqual(sum(p.startswith('receipts/records/') for p in self.calls), 1)
        self.app.history.sync()
        self.assertEqual(sum(p.startswith('receipts/records/') for p in self.calls), 1)
        reopened = Store(self.temp.name)
        try:
            history = PlatformHistory(reopened)
            with patch.object(history, 'fetch', side_effect=AssertionError('Offline local reads')):
                self.assertEqual(history.read('withdrawals')['data'][0]['id'], '1')
                self.assertEqual(len(history.read('receipts/mine')['data']), 2)
                self.assertEqual(history.read('receipts/records/1')['selected_sequence'], 1)
                self.assertEqual(history.read('receipts/records/2')['selected_sequence'], 2)
            raw = reopened.db.execute('SELECT payload FROM platform_history WHERE kind="withdrawals"').fetchone()[0]
            self.assertNotIn(b'fixture-address', raw)
        finally:
            reopened.db.close()

    def test_old_pending_status_updates_and_never_regresses(self):
        self.app.history.sync()
        self.remote_withdrawals = [withdrawal(3), withdrawal(2), withdrawal(1, 'paid', 3)]
        self.app.history.sync()
        self.assertEqual(self.app.history.read('withdrawals/1')['state'], 'paid')
        self.app.history.save_withdrawal(self.app.history.context(), withdrawal(1, 'processing', 2))
        self.assertEqual(self.app.history.read('withdrawals/1')['state'], 'paid')

    def test_failed_fetch_retains_history_and_independent_channel_continues(self):
        self.app.history.sync()
        def failing(context, path):
            if path == 'withdrawals':
                raise RuntimeError('platform temporarily offline')
            return self.fetch(context, path)
        self.remote_receipts.insert(0, receipt(3))
        self.app.history.fetch = failing
        self.assertFalse(self.app.history.sync()['saved'])
        self.assertEqual(len(self.app.history.read('withdrawals')['data']), 1)
        self.assertEqual(len(self.app.history.read('receipts/mine')['data']), 3)

    def test_profile_and_platform_switch_cannot_commit_or_read_previous_history(self):
        old = self.store.enrollment()['id']
        self.app.history.sync()
        context = self.app.history.context()
        secret = 'b' * 64
        new = 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32]
        self.app.api('POST', '/api/credentials', {'merchant_id': new, 'merchant_secret': secret})
        with self.assertRaises(RuntimeError):
            self.app.history.put(context, 'withdrawals', '1', withdrawal())
        self.assertEqual(self.app.history.read('receipts/mine')['data'], [])
        self.app.api('POST', '/api/merchant-profile', {'merchant_id': old})
        self.assertEqual(len(self.app.history.read('receipts/mine')['data']), 2)
        self.app.api('PUT', '/api/platform', {'platform_url': 'https://other.example'})
        self.assertEqual(self.app.history.read('receipts/mine')['data'], [])

    def test_history_read_requires_authenticated_matching_merchant_even_when_cached(self):
        self.app.history.sync()
        identity = self.store.enrollment()['id']
        with patch.object(self.app.platform_client, 'request', side_effect=AssertionError('No live reads')):
            self.assertEqual(self.app.platform_client.call('browser', 'merchant', 'GET', 'withdrawals', {}, '', identity).status, 401)
        session = login_fixture(self.app, 'browser')
        with patch.object(self.app.platform_client, 'request', side_effect=AssertionError('No live reads')):
            for path in ('me', 'withdrawals', 'withdrawals/1', 'receipts/mine', 'receipts/records/1'):
                self.assertEqual(self.app.platform_client.call('browser', 'merchant', 'GET', path, {}, '', identity).status, 200)
            self.app.platform_client.revoke('browser', session)
            self.assertEqual(self.app.platform_client.call('browser', 'merchant', 'GET', 'receipts/mine', {}, '', identity).status, 401)

    def test_login_wakes_background_sync_and_timer_runs_without_browser(self):
        stop = threading.Event()
        complete = threading.Event()
        def archive_sync(_):
            self.app.archive.last_attempt = time.monotonic()
            return {'saved': True}
        def history_sync(_):
            self.app.history.last_attempt = time.monotonic()
            complete.set()
            return {'saved': True}
        self.app.archive.last_attempt = self.app.history.last_attempt = time.monotonic()
        with patch.object(self.app.archive, 'sync', side_effect=archive_sync) as archive_call, \
             patch.object(self.app.history, 'sync', side_effect=history_sync) as history_call:
            thread = threading.Thread(target=self.app.archive_background, args=(stop,))
            thread.start()
            try:
                login_fixture(self.app, 'browser')
                self.assertTrue(complete.wait(3))
                complete.clear()
                # No page requests: the next scheduled period still downloads both channels.
                self.app.archive.last_attempt -= 301
                self.app.history.last_attempt -= 301
                self.assertTrue(complete.wait(3))
                self.assertEqual(archive_call.call_count, 2)
                self.assertEqual(history_call.call_count, 2)
            finally:
                stop.set();self.app.platform_wake.set();thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_successful_withdrawal_is_not_reported_as_failed_when_local_disk_write_fails(self):
        import sqlite3
        login_fixture(self.app, 'browser')
        with patch.object(self.app.platform_client, 'request', return_value=(201, withdrawal(), {})), \
             patch.object(self.app.history, 'save_withdrawal', side_effect=sqlite3.OperationalError('disk full')):
            result = self.app.platform_client.call('browser', 'merchant', 'POST', 'withdrawals', {},
                                                   'fixture-operation', self.store.enrollment()['id'])
        self.assertEqual(result.status, 201)
        self.assertEqual(json.loads(result.data)['id'], '1')
        self.assertTrue(self.app.platform_wake.is_set())
        self.assertTrue(self.app.history.error)

    def test_paginated_download_resumes_and_does_not_skip_older_rows(self):
        all_rows = [receipt(seq) for seq in range(451, 0, -1)]
        def fetch(context, path):
            if path.startswith('receipts/mine'):
                before = int(path.split('=')[-1]) if '?' in path else 1000
                rows = [r for r in all_rows if r['seq'] < before]
                return {'data': rows[:50], 'has_more': len(rows) > 50,
                        'next_before': rows[49]['seq'] if len(rows) > 50 else None}
            return self.fetch(context, path)
        self.app.history.fetch = fetch
        self.assertFalse(self.app.history.sync()['complete'])
        self.assertTrue(self.app.history.sync()['complete'])
        self.assertEqual(len(self.app.history.rows(self.app.history.context(), 'receipts')), 451)
        first = self.app.history.read('receipts/mine')
        second = self.app.history.read('receipts/mine?before=' + str(first['next_before']))
        self.assertEqual(first['data'][0]['seq'], 451)
        self.assertEqual(second['data'][0]['seq'], 401)

    def test_disk_failure_keeps_cursor_before_unpersisted_receipt(self):
        with patch.object(self.store, 'protect', side_effect=OSError('disk full')):
            result = self.app.history.sync()
        self.assertFalse(result['saved'])
        self.assertIsNone(self.app.history.get(self.app.history.context(), 'scan', 'receipts'))
        self.assertTrue(self.app.history.sync()['saved'])
        self.assertEqual(len(self.app.history.read('receipts/mine')['data']), 2)


if __name__ == '__main__':
    unittest.main()
