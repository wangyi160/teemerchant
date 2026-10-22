import copy
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
from app import Application
from platform_archive import PlatformArchive, platform_url
from store import Store


def page(store, amount='123', cursor=1):
    mid = store.enrollment()['id']
    return {'version': 1, 'archive_id': 'a'*32, 'merchant_id': mid, 'next_cursor': cursor, 'has_more': False,
            'data': [{'cursor': cursor, 'seq': 17, 'event': {'merchant_id': mid, 'model': 'grok-4.7',
                      'user_id': 'customer', 'created_at': 1792600000, 'refusal_type': 'provider_refusal',
                      'billing': {}, 'settlement': {'state': 'settled' if amount is not None else 'pending',
                       'charge_microusd': amount, 'official_cost_microusd': None}}}]}


class PlatformArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.settings.update(platform_url='https://platform.example', credentials_saved=True)
        self.store.save()
        self.app = Application(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_sync_and_restart_do_not_contact_router_or_double_count_legacy(self):
        self.store.archive('usage', 'old-router', [{'seq': 1, 'event': {'attempts': 0}}])
        with patch.object(self.app.transport, 'call', side_effect=AssertionError('No Router calls')):
            with patch.object(self.app.archive, 'fetch', return_value=page(self.store)) as fetch:
                self.app.sync()
                self.assertEqual(fetch.call_args.args[1], self.store.settings['token'])
            self.assertFalse(self.app.connected)
            report = self.app.api('GET', '/api/revenue', {})
            self.assertEqual(report['summary']['requests'], 1)
            self.assertEqual(report['summary']['charge_microusd'], '123')
            self.assertEqual(len(self.app.api('GET', '/api/records/refusals', {})['data']), 1)
        other = Store(self.temp.name)
        try:
            restored = PlatformArchive(other)
            self.assertEqual(restored.status()['cursor'], 1)
            self.assertEqual(restored.revenue('')['summary']['charge_microusd'], '123')
        finally:
            other.db.close()

    def test_manual_settlement_replaces_pending_instead_of_adding_income_twice(self):
        for value in (page(self.store, None), page(self.store, '9007199254740993', 2)):
            with patch.object(self.app.archive, 'fetch', return_value=value):
                self.app.sync()
        report = self.app.archive.revenue('')
        self.assertEqual(report['summary']['requests'], 1)
        self.assertEqual(report['summary']['pending'], 0)
        self.assertEqual(report['summary']['charge_microusd'], '9007199254740993')

    def test_failed_disk_write_never_advances_cursor(self):
        with patch.object(self.app.archive, 'fetch', return_value=page(self.store)):
            with patch.object(self.store, 'protect', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): self.app.sync()
        self.assertEqual(self.app.archive.status()['cursor'], 0)
        self.assertEqual(self.app.archive.records('usage'), [])

    def test_wrong_merchant_stream_or_nonprogressing_page_is_rejected(self):
        for mutate in (lambda p: p.update(merchant_id='m_other'),
                       lambda p: p['data'][0]['event'].update(merchant_id='m_other'),
                       lambda p: p.update(data=[], has_more=True, next_cursor=0)):
            value = page(self.store);mutate(value)
            with patch.object(self.app.archive, 'fetch', return_value=value):
                with self.assertRaises(RuntimeError): self.app.sync()
        self.assertEqual(self.app.archive.status()['cursor'], 0)

    def test_platform_failure_does_not_clear_router_status_or_send_credentials_to_router(self):
        self.app.connected = True
        self.app.last_error = 'router-specific'
        with patch.object(self.app.archive, 'fetch', side_effect=RuntimeError('platform offline')):
            with self.assertRaises(RuntimeError): self.app.sync()
        self.assertTrue(self.app.connected)
        self.assertEqual(self.app.last_error, 'router-specific')
        self.assertEqual(self.app.archive.status()['error'], 'platform offline')

    def test_manual_sync_does_not_report_success_while_background_download_is_busy(self):
        with self.app.archive.lock:
            with self.assertRaisesRegex(RuntimeError, '正在同步'):
                self.app.api('POST', '/api/sync', {})
        self.assertEqual(self.app.archive.status()['cursor'], 0)

    def test_profile_change_during_download_does_not_commit_old_archive(self):
        def fetch(*args):
            result = page(self.store)
            self.app.archive.reset()
            return result
        with patch.object(self.app.archive, 'fetch', side_effect=fetch):
            with self.assertRaises(RuntimeError): self.app.sync()
        self.assertEqual(self.app.archive.status()['cursor'], 0)

    def test_only_platform_secret_is_sent_and_redirects_are_not_followed(self):
        secret = self.store.settings['token']
        owner = self.store.control_token().split('.')[1]
        seen = []
        def handler(request):
            seen.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer '+secret)
            self.assertNotIn(owner, str(request.headers))
            return httpx.Response(302, headers={'Location': 'https://other.example'})
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        with patch('platform_archive.httpx.Client', return_value=client):
            with self.assertRaises(RuntimeError): self.app.archive.fetch('https://platform.example', secret, 0)
        self.assertEqual(len(seen), 1)

    def test_router_archive_endpoints_are_disabled(self):
        for method, path in [('GET','/merchant/usage?limit=500'),('POST','/merchant/usage/ack'),('DELETE','/merchant/refusals')]:
            with self.assertRaises(ValueError): self.app.transport.call(method, path)

    def test_platform_configuration_rejects_remote_plaintext_and_keeps_router_settings(self):
        for value in ('http://192.168.1.7:8080', 'https://u:p@example.com', 'https://example.com/portal', 'https://example.com?key=x'):
            with self.assertRaises(ValueError): platform_url(value)
        self.assertEqual(platform_url('http://127.0.0.1:8080/'), 'http://127.0.0.1:8080')
        before = self.store.settings['router']
        self.app.api('PUT', '/api/platform', {'platform_url': 'http://127.0.0.1:8080'})
        self.assertEqual(self.store.settings['router'], before)


if __name__ == '__main__': unittest.main()
