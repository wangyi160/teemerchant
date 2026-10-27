"""Exercise timed recovery with fake clocks, providers and Router; no live tokens."""
import copy
import json
import time
import unittest
from unittest.mock import patch
from app import Application
from account_auth import AccountError
from credential_protocol import fingerprint
from oauth_network import OAuthError
from store import Store
from test_control_fixture import install
import test_account_sync as fixtures


class RenewalRecoveryTests(unittest.TestCase):
    tearDown = fixtures.AccountSyncTests.tearDown

    def setUp(self):
        fixtures.AccountSyncTests.setUp(self)
        fixtures.AccountSyncTests.installed(self)
        self.app.sync_credentials()
        self.check.reset_mock()
        self.router.access_calls.clear()
        self.now = int(time.time())
        self.clock = patch('local_accounts.time.time', return_value=self.now).start()
        self.addCleanup(patch.stopall)
        self.book.data['routers'][self.url]['accounts']['bad']['credential'].update(
            refresh_token='OLD_PRIVATE_REFRESH', expires_at=self.now + 10)
        self.rotated = {'access_token':'NEW_PRIVATE_ACCESS', 'refresh_token':'ROTATED_PRIVATE_REFRESH',
                        'expires_at':self.now + 3600}
        self.refresh = patch('oauth_service.refresh', return_value=self.rotated).start()

    def sync(self, now=None):
        if now is not None:
            self.clock.return_value = now
        self.app.sync_credentials()
        return self.book.view(self.url)['account_sync']['bad']

    def reload(self):
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.router.store = self.store
        self.book = self.store.accounts
        self.app = install(Application(self.store, self.router), self.router)
        self.app.accept(self.router.remote)

    def test_check_timeout_resumes_check_with_saved_rotated_token_after_restart(self):
        self.check.side_effect = AccountError('timeout')
        failed = self.sync()
        self.assertEqual((failed['state'], failed['step'], failed['retry_at']), ('retry','check',self.now+30))
        self.assertEqual(self.book.data['routers'][self.url]['accounts']['bad']['credential'], self.rotated)
        self.assertEqual(len(self.router.remote['pools'][0]['deployments']), 2)
        self.assertEqual(self.router.access_calls, [])
        self.assertNotIn(b'ROTATED_PRIVATE_REFRESH', self.book.path.read_bytes())
        self.reload()
        self.sync(self.now+29)
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(self.check.call_count, 1)
        self.check.side_effect = None
        done = self.sync(self.now+30)
        self.assertEqual(done['state'], 'synced')
        self.assertNotIn('failures', done)
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(self.router.access_calls, ['bad'])
        self.assertEqual(self.book.view(self.url)['access_token_sync']['bad'],
                         {'last_success_at':self.now+30, 'current':True})

    def test_upload_retries_exactly_four_times_then_pauses_and_manual_retry_keeps_step(self):
        self.router.rejected.add('bad')
        at = self.now
        state = self.sync(at)
        for number, delay in enumerate((30,60,120,300), 1):
            self.assertEqual((state['state'], state['step'], state['failures']), ('retry','upload',number))
            self.assertEqual(state['retry_at'], at+delay)
            self.sync(at+delay-1)
            self.assertEqual(self.router.access_calls.count('bad'), number)
            self.assertEqual(len(self.router.remote['pools'][0]['deployments']), 2)
            at += delay
            state = self.sync(at)
        self.assertEqual((state['state'], state['failures']), ('paused',5))
        self.sync(at+1000)
        self.assertEqual(self.router.access_calls.count('bad'), 5)
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(self.check.call_count, 1)
        self.assertNotIn('PRIVATE', json.dumps(state))
        self.router.rejected.clear()
        self.app.api('POST','/api/accounts/retry',{'id':'bad'})
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'], 'synced')
        self.assertEqual(self.check.call_count, 1)
        self.assertEqual(self.refresh.call_count, 1)

    def test_budget_is_shared_across_failed_steps_and_refresh_is_not_replayed(self):
        self.refresh.side_effect = OAuthError('connect failed')
        first = self.sync()
        self.assertEqual((first['step'],first['retry_at']), ('refresh',self.now+30))
        self.sync(self.now+29)
        self.assertEqual(self.refresh.call_count, 1)
        self.refresh.side_effect = None
        self.check.side_effect = AccountError('timeout')
        second = self.sync(self.now+30)
        self.assertEqual((second['step'],second['failures'],second['retry_at']), ('check',2,self.now+90))
        self.check.side_effect = None
        self.router.rejected.add('bad')
        third = self.sync(self.now+90)
        self.assertEqual((third['step'],third['failures'],third['retry_at']), ('upload',3,self.now+210))
        self.router.rejected.clear()
        self.assertEqual(self.sync(self.now+210)['state'], 'synced')
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(self.router.access_calls, ['bad','bad'])

    def test_refresh_connection_failures_exhaust_budget_without_auth_checks_or_uploads(self):
        self.refresh.side_effect = OAuthError('connect failed')
        state = self.sync()
        for _ in range(4):
            state = self.sync(state['retry_at'])
        self.assertEqual(state['state'], 'paused')
        self.assertEqual(self.refresh.call_count, 5)
        self.check.assert_not_called()
        self.assertEqual(self.router.access_calls, [])
        self.sync(self.now+2000)
        self.assertEqual(self.refresh.call_count, 5)

    def test_uncertain_refresh_response_retries_four_times_then_allows_manual_refresh(self):
        self.refresh.side_effect = OAuthError('lost response', uncertain=True)
        state = self.sync()
        self.assertEqual((state['state'],state['code']), ('retry','renewal_uncertain'))
        self.reload()
        at = self.now
        for attempt, delay in enumerate((30,60,120,300),1):
            self.assertEqual(state['retry_at'],at+delay)
            self.sync(at+delay-1)
            self.assertEqual(self.refresh.call_count,attempt)
            at += delay
            state = self.sync(at)
        self.assertEqual((state['state'],state['failures']),('paused',5))
        self.sync(at+1000)
        self.assertEqual(self.refresh.call_count,5)
        self.check.assert_not_called()
        self.refresh.side_effect = None
        self.app.api('POST','/api/accounts/refresh',{'id':'bad'})
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'], 'synced')
        self.assertEqual(self.refresh.call_count,6)
        history = self.book.view(self.url)['account_errors']['bad']
        self.assertEqual([r['attempt'] for r in history],[0,1,2,3,4])
        self.assertTrue(all(r['code']=='renewal_uncertain' and r['step']=='refresh' for r in history))
        self.reload()
        self.assertEqual(self.book.view(self.url)['account_errors']['bad'],history)

    def test_process_crash_during_refresh_uses_same_schedule(self):
        name, account, basis = self.book.renewal_candidates(self.url)[0]
        self.book.renewal_started(self.url,name,basis)
        self.reload()
        state = self.sync()
        self.assertEqual((state['state'],state['code'],state['retry_at']),('retry','renewal_interrupted',self.now+30))
        self.refresh.assert_not_called()
        self.assertEqual(self.sync(self.now+30)['state'],'synced')
        self.assertEqual(self.refresh.call_count,1)

    def test_manual_refresh_of_valid_token_does_not_change_expiry_to_force_refresh(self):
        credential = self.book.data['routers'][self.url]['accounts']['bad']['credential']
        credential['expires_at'] = self.now+7200
        before = copy.deepcopy(credential)
        self.app.api('POST','/api/accounts/refresh',{'id':'bad'})
        self.assertEqual(self.refresh.call_args.args[1],before)
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'],'synced')
        self.assertEqual(self.refresh.call_count,1)

    def test_manual_refresh_during_upload_backoff_resumes_upload_and_keeps_error_history(self):
        self.router.rejected.add('bad')
        self.sync()
        self.router.rejected.clear()
        self.app.api('POST','/api/accounts/refresh',{'id':'bad'})
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'],'synced')
        self.assertEqual(self.refresh.call_count,1)
        self.assertEqual(self.check.call_count,1)
        row=self.book.view(self.url)['account_errors']['bad'][0]
        self.assertEqual((row['step'],row['code']),('upload','sync_failed'))
        self.assertIn('account_credential_changed',row['error'])
        self.assertNotIn('PRIVATE',json.dumps(row))

    def test_manual_refresh_cannot_enable_disabled_account_or_refresh_access_only_account(self):
        with self.assertRaisesRegex(ValueError,'Refresh Token'):
            self.app.api('POST','/api/accounts/refresh',{'id':'good'})
        self.book.data['routers'][self.url]['accounts']['bad']['enabled']=False
        with self.assertRaisesRegex(ValueError,'已停用'):
            self.app.api('POST','/api/accounts/refresh',{'id':'bad'})
        self.refresh.assert_not_called()

    def test_manual_refresh_resumes_failed_upload_even_without_an_automatic_renewal(self):
        credential = self.book.data['routers'][self.url]['accounts']['bad']['credential']
        credential['expires_at'] = self.now+7200
        credential['access_token'] = 'MANUALLY_IMPORTED_TOKEN'
        self.router.rejected.add('bad')
        self.sync()
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'],'paused')
        self.router.rejected.clear()
        self.app.api('POST','/api/accounts/refresh',{'id':'bad'})
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'],'synced')
        self.assertEqual(self.check.call_count,1)
        self.refresh.assert_not_called()

    def test_lost_upload_ack_retries_same_token_without_refresh_or_auth_probe(self):
        original = self.router.call
        sent = []
        def call(method, path, body=None):
            value = original(method, path, body)
            if path == '/merchant/accounts/access':
                sent.append(copy.deepcopy(body))
                if len(sent) == 1:
                    raise RuntimeError('merchant_request_timeout')
            return value
        with patch.object(self.router, 'call', side_effect=call):
            self.assertEqual(self.sync()['step'], 'upload')
            self.reload()
            self.assertEqual(self.sync(self.now+30)['state'], 'synced')
        self.assertEqual(sent[0]['accounts']['bad']['credential'],sent[1]['accounts']['bad']['credential'])
        self.assertNotIn('REFRESH', json.dumps(sent))
        self.assertEqual(self.refresh.call_count,1)
        self.assertEqual(self.check.call_count,1)

    def test_other_account_and_configuration_sync_cannot_bypass_wait(self):
        self.check.side_effect = lambda a: (_ for _ in ()).throw(AccountError('timeout')) if a['credential']['access_token']=='NEW_PRIVATE_ACCESS' else None
        self.sync()
        config = copy.deepcopy(self.config)
        view = self.book.view(self.url)
        config.update(expected_revision=view['settings']['revision'],_local_revision=view['local_revision'],
                      accounts={'good':{'credential':{'access_token':'GOOD_REPLACEMENT','expires_at':self.now+3600}}})
        self.app.api('PUT','/api/config',config)
        self.assertEqual(self.book.view(self.url)['account_sync']['bad']['state'], 'retry')
        self.assertEqual(self.book.view(self.url)['account_sync']['good']['state'], 'synced')
        self.assertNotIn('bad', self.router.writes[-1]['accounts'])
        self.assertEqual(len(self.router.remote['pools'][0]['deployments']), 2)
        self.check.side_effect = None
        self.assertEqual(self.sync(self.now+30)['state'], 'synced')

    def test_router_restart_during_upload_backoff_recovers_only_at_deadline(self):
        self.router.rejected.add('bad')
        self.sync()
        self.router.rejected.clear()
        self.router.remote['merchant'].update(runtime_id='new-runtime',configuration_required=True)
        self.router.remote['pools'] = []
        self.router.remote['accounts']['data'] = []
        self.app.connected = False
        self.app.connect()
        self.assertFalse(any(a['name']=='bad' for a in self.router.remote['accounts']['data']))
        self.assertEqual(self.sync(self.now+30)['state'], 'synced')
        self.assertEqual(self.refresh.call_count,1)
        self.assertEqual(self.check.call_count,1)

    def test_new_cycle_has_fresh_budget_and_manual_credential_change_clears_old_retry(self):
        self.router.rejected.add('bad')
        self.sync()
        self.router.rejected.clear()
        self.sync(self.now+30)
        self.rotated = {**self.rotated,'access_token':'SECOND_ACCESS','expires_at':self.now+7200}
        self.refresh.return_value = self.rotated
        self.router.rejected.add('bad')
        second = self.sync(self.now+3400)
        self.assertEqual((second['state'],second['failures']),('retry',1))
        self.book.data['routers'][self.url]['accounts']['bad']['credential'] = {
            'access_token':'MANUALLY_REPLACED', 'expires_at':self.now+7200}
        self.assertEqual(self.book.sync_accounts(self.url)['bad']['sync'],{})
        self.router.rejected.clear()
        self.assertEqual(self.sync()['state'],'synced')

    def test_crashed_check_or_upload_resumes_that_step_without_new_rotation(self):
        for interrupted in ('checking','sending'):
            with self.subTest(interrupted=interrupted):
                self.app.renew_credentials()
                account = self.book.sync_accounts(self.url)['bad']
                self.book.account_sync_result(self.url,'bad',account['basis'],interrupted)
                self.reload()
                failed = self.sync()
                self.assertEqual((failed['state'],failed['step']),('retry','check' if interrupted=='checking' else 'upload'))
                self.assertEqual(self.sync(failed['retry_at'])['state'],'synced')
                self.assertEqual(self.refresh.call_count,1 if interrupted=='checking' else 2)
                self.book.data['routers'][self.url]['accounts']['bad']['credential']['expires_at'] = self.now+10
        self.assertEqual(self.check.call_count,1)

    def test_pending_configuration_upload_resumes_without_rechecking_rotated_token(self):
        config = copy.deepcopy(self.config)
        view = self.book.view(self.url)
        config.update(expected_revision=view['settings']['revision'],_local_revision=view['local_revision'],accounts={})
        config['pools'][0]['deployments'][0]['rpm'] = 23
        self.book.stage(self.url,config)
        self.router.failure = True
        failed = self.sync()
        self.assertEqual((failed['state'],failed['step']),('retry','upload'))
        self.router.failure = False
        self.assertEqual(self.sync(self.now+30)['state'],'synced')
        self.assertEqual(self.refresh.call_count,1)
        self.assertEqual(self.check.call_count,1)
        self.assertEqual(self.router.remote['pools'][0]['deployments'][0]['rpm'],23)


if __name__ == '__main__': unittest.main()
