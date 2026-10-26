"""Account holds are durable and isolated; provider catalogs are not auth evidence."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx
from app import Application
from test_control_fixture import install
from account_auth import AccountError, verify, sync_error
from credential_protocol import PROTOCOL, fingerprint
from store import Store
from test_local_accounts import Router


class SyncRouter(Router):
    def __init__(self, store):
        super().__init__(store)
        self.remote['merchant'].update(credential_protocol=PROTOCOL, runtime_id='runtime')
        self.access_calls = []
        self.rejected = set()

    def call(self, method, path, body=None):
        if path == '/merchant/bind':
            owner = hashlib.sha256(self.store.control_token().split('.')[1].encode()).hexdigest()
            self.remote['merchant']['owner_hash'] = owner
            return {'merchant_id': self.store.enrollment()['id'], 'owner_hash': owner, 'merchant_control_version': 1}
        if path == '/merchant/accounts/access':
            name = next(iter(body['accounts']))
            self.access_calls.append(name)
            if name in self.rejected:
                raise RuntimeError('Router 返回 409：account_credential_changed PRIVATE_TOKEN')
            item = next(a for a in self.remote['accounts']['data'] if a['name'] == name)
            item['access_sha256'] = fingerprint(body['accounts'][name]['credential'])
        elif method == 'PUT':
            if self.failure:
                raise RuntimeError('Router 返回 409：revision_conflict PRIVATE_TOKEN')
            self.writes.append(copy.deepcopy(body))
            self.remote['pools'] = copy.deepcopy(body['pools'])
            self.remote['settings'].update({k: copy.deepcopy(body[k]) for k in ('pool_pricing', 'max_concurrent', 'rpm', 'tpm')})
            self.remote['settings']['revision'] += 1
            self.remote['merchant']['configuration_required'] = False
            for name, account in body.get('accounts', {}).items():
                account = {**self.store.accounts.data['routers']['https://fixture.example']['accounts'][name], **account}
                self.remote['accounts']['data'] = [a for a in self.remote['accounts']['data'] if a['name'] != name]
                self.remote['accounts']['data'].append({'name': name, 'enabled': account.get('enabled', True),
                    'access_sha256': fingerprint(account['credential']), 'health': {'state': 'active'}})
        return copy.deepcopy(self.remote)


class AccountSyncTests(unittest.TestCase):
    def test_sync_diagnostics_preserve_categories_without_secrets(self):
        for code in ('merchant_request_timeout','router_attestation_failed','invalid_session'):
            kind,message=sync_error(RuntimeError(code+' https://proxy:PRIVATE_TOKEN@example.test'))
            self.assertEqual(kind,'sync_failed')
            self.assertIn(code,message)
            self.assertNotIn('PRIVATE_TOKEN',message)
            self.assertNotIn('example.test',message)
        kind,message=sync_error(KeyError('PRIVATE_TOKEN'))
        self.assertIn('KeyError',message)
        self.assertNotIn('PRIVATE_TOKEN',message)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.url = 'https://fixture.example'
        self.store.settings['router'] = self.url
        self.store.save()
        self.router = SyncRouter(self.store)
        self.app = install(Application(self.store, self.router), self.router)
        self.app.accept(self.router.remote)
        self.book = self.store.accounts
        self.verifier = patch('app.verify_account')
        self.check = self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.config = {'expected_revision': 1, '_local_revision': 0,
            'pools': [{'name': 'claude', 'provider': 'anthropic', 'allowed_models': ['claude-test'],
                'deployments': [{'id': n, 'oauth_account': n, 'provider': 'anthropic', 'enabled': True,
                                 'max_concurrent': 4, 'rpm': 0} for n in ('bad', 'good')]}],
            'accounts': {n: {'provider': 'anthropic', 'enabled': True, 'proxy': 'http://proxy.example:80',
                'credential': {'access_token': n + '_PRIVATE_TOKEN', 'expires_at': time.time() + 3600}}
                for n in ('bad', 'good')},
            'customers': [], 'customer_defaults': self.router.remote['settings']['customer_defaults'],
            'pool_pricing': [{'pool': 'claude', 'multiplier_millionths': 1000000}],
            'max_concurrent': 16, 'rpm': 0, 'tpm': 0}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def installed(self):
        self.book.stage(self.url, self.config)
        self.router.remote['pools'] = copy.deepcopy(self.config['pools'])
        self.router.remote['settings']['pool_pricing'] = copy.deepcopy(self.config['pool_pricing'])
        self.router.remote['accounts']['data'] = [{'name': n, 'enabled': True,
            'access_sha256': fingerprint({'access_token': 'OLD_' + n}), 'health': {'state': 'active'}}
            for n in ('bad', 'good')]
        self.book.acknowledge(self.url, self.router.remote)
        self.app.accept(self.router.remote)

    def test_bad_auth_is_excluded_and_healthy_account_is_published(self):
        self.check.side_effect = lambda a: (_ for _ in ()).throw(AccountError('authentication_failed', 'HTTP 401')) if a['credential']['access_token'].startswith('bad') else None
        result = self.app.api('PUT', '/api/config', self.config)
        self.assertEqual(result['account_sync']['bad']['code'], 'authentication_failed')
        self.assertEqual(result['account_sync']['good']['state'], 'synced')
        self.assertEqual(len(result['pools'][0]['deployments']), 2)
        sent = self.router.writes[-1]
        self.assertEqual(list(sent['accounts']), ['good'])
        self.assertEqual([d['id'] for d in sent['pools'][0]['deployments']], ['good'])
        for _ in range(3): self.app.sync_credentials()
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(len(self.router.writes), 1)
        reloaded = Store(self.tmp.name)
        try:
            self.assertEqual(reloaded.accounts.view(self.url)['account_sync']['bad']['state'], 'paused')
            self.assertEqual(reloaded.accounts.account(self.url, 'bad')['credential_present'], True)
        finally: reloaded.db.close()

    def test_account_save_succeeds_with_platform_completely_unavailable(self):
        with patch.object(self.app.platform_data, 'configure', side_effect=AssertionError('account save reached platform')) as configure:
            result = self.app.api('PUT', '/api/config', self.config)
        configure.assert_not_called()
        self.assertEqual(result['local_sync']['state'], 'synced')
        self.assertTrue(all(v['state']=='synced' for v in result['account_sync'].values()))
        with patch.object(self.app.platform_data, 'notify_synced', side_effect=RuntimeError('offline')):
            self.app.notify_snapshot()
        self.assertTrue(all(v['state']=='synced' for v in self.app.remote['account_sync'].values()))

    def test_manual_retry_does_not_require_platform_queries(self):
        self.installed()
        account = self.book.sync_accounts(self.url)['bad']
        self.book.account_sync_result(self.url, 'bad', account['basis'], 'paused', 'sync_failed', 'old failure')
        with patch.object(self.app, 'platform_state', side_effect=RuntimeError('platform offline')) as query:
            result = self.app.api('POST', '/api/accounts/retry', {'id': 'bad'})
        query.assert_not_called()
        self.assertEqual(result['account_sync']['bad']['state'], 'synced')

    def test_access_upload_errors_pause_one_account_without_blocking_others(self):
        self.installed()
        self.router.rejected.add('bad')
        self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good'])
        result = self.app.remote['account_sync']
        self.assertEqual(result['bad']['state'], 'paused')
        self.assertIn('account_credential_changed', result['bad']['error'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))
        self.assertEqual(result['good']['state'], 'synced')
        for _ in range(3): self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good'])
        self.assertEqual(len(self.router.writes), 1)
        self.router.rejected.clear()
        self.app.api('POST', '/api/accounts/retry', {'id': 'bad'})
        self.assertEqual(self.app.remote['account_sync']['bad']['state'], 'synced')
        self.assertEqual(len(self.router.remote['pools'][0]['deployments']), 2)

    def test_paused_account_is_not_restored_after_router_restart(self):
        self.test_bad_auth_is_excluded_and_healthy_account_is_published()
        self.router.remote['merchant']['configuration_required'] = True
        self.router.remote['pools'] = []
        self.router.remote['accounts']['data'] = []
        self.app.connected = False
        self.app.connect()
        self.assertNotIn('bad', next(w for w in reversed(self.router.writes) if 'accounts' in w)['accounts'])
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(len(self.app.remote['pools'][0]['deployments']), 2)

    def test_only_changed_material_resumes_and_does_not_clear_other_holds(self):
        self.installed()
        self.check.side_effect = AccountError('connection_failed')
        self.app.sync_credentials()
        config = copy.deepcopy(self.config)
        view = self.book.view(self.url)
        config.update(expected_revision=view['settings']['revision'], _local_revision=view['local_revision'])
        config['accounts'] = {'bad': {'proxy': 'http://new-proxy.example:80'}}
        self.book.stage(self.url, config)
        states = self.book.sync_accounts(self.url)
        self.assertEqual(states['bad']['sync'], {})
        self.assertEqual(states['good']['sync']['state'], 'paused')
        self.check.side_effect = None
        self.app.sync_credentials()
        self.assertEqual(self.app.remote['account_sync']['bad']['state'], 'synced')
        self.assertEqual(self.app.remote['account_sync']['good']['state'], 'paused')

    def test_initial_offline_save_automatically_publishes_once(self):
        self.app.connected = False
        self.app.api('PUT', '/api/config', self.config)
        self.assertEqual(self.router.writes, [])
        self.app.connected = True
        self.app.sync_credentials()
        self.assertEqual(len(self.router.writes), 1)
        self.app.sync_credentials()
        self.assertEqual(len(self.router.writes), 1)

    def test_config_failure_is_durable_and_not_retried_each_tick(self):
        self.router.failure = True
        self.app.api('PUT', '/api/config', self.config)
        for a in self.app.remote['account_sync'].values(): self.assertEqual(a['state'], 'paused')
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(self.app.remote))
        self.router.failure = False
        for _ in range(3): self.app.sync_credentials()
        self.assertEqual(self.router.writes, [])
        self.app.api('POST', '/api/accounts/retry', {'id': 'good'})
        self.assertEqual(list(next(w for w in reversed(self.router.writes) if 'accounts' in w)['accounts']), ['good'])

    def test_crash_marker_and_expired_token_are_not_waiting_for_catalog(self):
        self.installed()
        a = self.book.sync_accounts(self.url)['bad']
        self.book.account_sync_result(self.url, 'bad', a['basis'], 'sending')
        self.book.data['routers'][self.url]['accounts']['good']['credential']['expires_at'] = 1
        self.app.sync_credentials()
        self.assertEqual(self.app.remote['account_sync']['bad']['code'], 'sync_uncertain')
        self.assertEqual(self.app.remote['account_sync']['good']['code'], 'access_token_expired')
        self.check.assert_not_called()

    def test_shared_catalog_cannot_hide_account_auth_failure(self):
        self.installed()
        self.router.remote['accounts']['data'][0].update(access_sha256=fingerprint(self.config['accounts']['bad']['credential']), health={'state':'reauthorization_required'})
        self.app.accept(self.router.remote)
        self.app.sync_credentials()
        self.assertEqual(self.app.remote['account_sync']['bad']['code'], 'authentication_failed')
        self.assertEqual(self.check.call_count, 1)

    def test_renewal_error_pauses_and_never_reuses_refresh_automatically(self):
        from oauth_network import OAuthError
        self.installed()
        a = self.book.data['routers'][self.url]['accounts']['bad']['credential']
        a.update(refresh_token='PRIVATE_REFRESH', expires_at=time.time() + 10)
        with patch('oauth_service.refresh', side_effect=OAuthError('timeout')) as renewal:
            for _ in range(3): self.app.sync_credentials()
        self.assertEqual(renewal.call_count, 1)
        self.assertEqual(self.app.remote['account_sync']['bad']['code'], 'renewal_failed')

    def test_legacy_remote_account_without_local_material_is_not_withdrawn(self):
        self.installed()
        self.book.data['routers'][self.url]['accounts'].pop('bad')
        self.app.sync_credentials()
        self.assertNotIn('bad', self.app.remote['account_sync'])
        self.assertEqual(len(self.router.remote['pools'][0]['deployments']), 2)
        self.assertEqual(self.router.access_calls, ['good'])
        self.assertIn('bad', self.app.remote['local_sync']['missing_credentials'])

    def test_restart_missing_one_credential_still_restores_other_accounts(self):
        self.installed()
        self.book.data['routers'][self.url]['accounts'].pop('bad')
        self.router.remote['merchant']['configuration_required'] = True
        self.router.remote['pools'] = []
        self.router.remote['accounts']['data'] = []
        self.app.connected = False
        self.app.connect()
        self.assertEqual(list(next(w for w in reversed(self.router.writes) if 'accounts' in w)['accounts']), ['good'])
        self.assertEqual(self.app.remote['account_sync']['bad']['code'], 'invalid_credential')
        self.assertEqual(len(self.app.remote['pools'][0]['deployments']), 2)

    def test_new_token_is_persisted_and_sent_instead_of_stale_pending_token(self):
        self.config['accounts']['bad']['credential'].update(refresh_token='PRIVATE_REFRESH', expires_at=time.time() + 10)
        new = {'access_token': 'ROTATED', 'refresh_token': 'NEW_PRIVATE_REFRESH', 'expires_at': time.time() + 3600}
        with patch('oauth_service.refresh', return_value=new):
            self.app.api('PUT', '/api/config', self.config)
        sent = self.router.writes[-1]['accounts']['bad']['credential']
        self.assertEqual(sent['access_token'], 'ROTATED')
        self.assertNotIn('refresh', json.dumps(sent))
        self.assertEqual(self.book.data['routers'][self.url]['accounts']['bad']['credential'], new)

    def test_manual_retry_connection_failure_keeps_account_paused(self):
        self.installed()
        with patch.object(self.router, 'call', side_effect=RuntimeError('merchant_request_failed PRIVATE_TOKEN')):
            result = self.app.api('POST', '/api/accounts/retry', {'id': 'bad'})
        self.assertEqual(result['account_sync']['bad']['state'], 'paused')
        self.assertIn('merchant_request_failed', result['account_sync']['bad']['error'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))

    def test_config_only_account_change_failure_also_pauses_that_account(self):
        self.installed()
        self.app.sync_credentials()
        update = copy.deepcopy(self.config)
        update.update(_local_revision=self.app.remote['local_revision'], expected_revision=self.app.remote['settings']['revision'], accounts={})
        update['pools'][0]['deployments'][0]['rpm'] = 12
        self.router.failure = True
        self.app.api('PUT', '/api/config', update)
        self.assertEqual(self.app.remote['account_sync']['bad']['state'], 'paused')
        self.assertEqual(self.app.remote['account_sync']['good']['state'], 'synced')

    def test_token_time_advances_only_for_successful_upload_and_survives_failure(self):
        self.installed()
        now = int(time.time())
        with patch('local_accounts.time.time', return_value=now):
            self.app.sync_credentials()
        expected = {'last_success_at': now, 'current': True}
        self.assertEqual(self.app.remote['access_token_sync']['good'], expected)
        with patch('local_accounts.time.time', return_value=now + 10):
            self.app.check_accounts()
            self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good'])
        self.assertEqual(self.app.remote['access_token_sync']['good'], expected)
        self.book.data['routers'][self.url]['accounts']['bad']['credential']['access_token'] = 'REPLACEMENT'
        self.router.rejected.add('bad')
        with patch('local_accounts.time.time', return_value=now + 20):
            self.app.sync_credentials()
        self.assertEqual(self.app.remote['access_token_sync']['bad'], {'last_success_at': now, 'current': False})
        self.assertEqual(self.app.remote['access_token_sync']['good'], expected)
        self.book.retry_account(self.url, 'bad')
        self.assertEqual(self.book.view(self.url)['access_token_sync']['bad']['last_success_at'], now)
        reloaded = Store(self.tmp.name)
        try: self.assertEqual(reloaded.accounts.view(self.url)['access_token_sync']['good'], expected)
        finally: reloaded.db.close()

    def test_config_saves_without_token_do_not_change_token_sync_time(self):
        now = int(time.time())
        with patch('local_accounts.time.time', return_value=now):
            self.app.api('PUT', '/api/config', self.config)
        self.assertEqual(self.app.remote['access_token_sync']['good']['last_success_at'], now)
        update = copy.deepcopy(self.config)
        update.update(accounts={}, expected_revision=self.app.remote['settings']['revision'], _local_revision=self.app.remote['local_revision'])
        update['pools'][0]['deployments'][1]['rpm'] = 20
        with patch('local_accounts.time.time', return_value=now + 30):
            self.app.api('PUT', '/api/config', update)
        self.assertEqual(self.app.remote['access_token_sync']['good']['last_success_at'], now)
        self.assertEqual(self.router.writes[-1]['accounts'], {})

    def test_existing_tokens_without_history_confirm_once_per_runtime(self):
        self.installed()
        for a in self.router.remote['accounts']['data']:
            a['access_sha256'] = fingerprint(self.config['accounts'][a['name']]['credential'])
        self.app.accept(self.router.remote)
        self.assertIsNone(self.app.remote['access_token_sync']['good']['last_success_at'])
        self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good'])
        self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good'])
        self.router.remote['merchant']['runtime_id'] = 'new-runtime'
        self.app.accept(self.router.remote)
        self.assertFalse(self.app.remote['access_token_sync']['good']['current'])
        self.app.sync_credentials()
        self.assertEqual(self.router.access_calls, ['bad', 'good', 'bad', 'good'])
        self.assertTrue(self.app.remote['access_token_sync']['good']['current'])

    def test_no_success_time_without_matching_router_acknowledgement(self):
        self.installed()
        self.book.confirm_token_upload(self.url, self.config['accounts'], self.router.remote)
        self.assertIsNone(self.book.view(self.url)['access_token_sync']['good']['last_success_at'])
        for a in self.router.remote['accounts']['data']:
            a['access_sha256'] = fingerprint(self.config['accounts'][a['name']]['credential'])
        self.book.confirm_token_upload(self.url, {'good': {'enabled': True}}, self.router.remote)
        self.assertIsNone(self.book.view(self.url)['access_token_sync']['good']['last_success_at'])


class AuthenticationTests(unittest.TestCase):
    def account(self, provider='anthropic'):
        return {'provider':provider, 'oauth_account':'a', 'credential':{'access_token':'PRIVATE_TOKEN', 'refresh_token':'PRIVATE_REFRESH'},
                'proxy':'socks5h://user:PRIVATE_PROXY@proxy.example:1080'}

    def request(self, response, provider='anthropic'):
        seen = []
        def handle(request):
            seen.append(request)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch('account_auth.httpx.Client', return_value=client) as factory:
            verify(self.account(provider))
        self.assertFalse(factory.call_args.kwargs['trust_env'])
        self.assertFalse(factory.call_args.kwargs['follow_redirects'])
        self.assertEqual(factory.call_args.kwargs['proxy'], self.account()['proxy'])
        self.assertEqual(seen[0].method, 'GET')
        self.assertNotIn('PRIVATE_REFRESH', str(seen[0].headers))
        self.assertEqual(seen[0].content, b'')
        return seen[0]

    def test_official_read_only_checks_use_account_proxy_and_expected_headers(self):
        for provider, field, host in [('anthropic','data','api.anthropic.com'), ('openai','models','chatgpt.com'), ('xai','models','api.x.ai')]:
            request = self.request(httpx.Response(200, json={field:[]}), provider)
            self.assertEqual(request.url.host, host)
            if provider == 'anthropic': self.assertEqual(request.headers['anthropic-beta'], 'oauth-2025-04-20')

    def test_status_reasons_are_distinct_and_never_echo_response(self):
        for status, code in [(401,'authentication_failed'),(403,'permission_denied'),(407,'proxy_authentication_failed'),(429,'rate_limited'),(502,'upstream_unavailable'),(302,'upstream_unavailable')]:
            with self.assertRaises(AccountError) as failure:
                self.request(httpx.Response(status, text='PRIVATE_TOKEN PRIVATE_PROXY'))
            self.assertEqual(failure.exception.code, code)
            self.assertNotIn('PRIVATE', str(failure.exception))

    def test_invalid_response_is_not_success_or_authentication_failure(self):
        for response in (httpx.Response(200,text='<html>login</html>'), httpx.Response(200,json={'error':'PRIVATE'})):
            with self.assertRaises(AccountError) as failure: self.request(response)
            self.assertEqual(failure.exception.code, 'invalid_response')


if __name__ == '__main__': unittest.main()
