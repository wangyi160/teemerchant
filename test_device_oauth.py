"""Isolated official-device-flow protocol, lifecycle and proxy boundary tests."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from oauth_network import OAuthError, request
from oauth_service import OAuthService, PROVIDERS


DEVICE = {'device_auth_id':'PRIVATE_DEVICE_ID','user_code':'ABCD-EFGH','interval':'5'}
APPROVED = {'authorization_code':'PRIVATE_CODE','code_verifier':'PRIVATE_VERIFIER','code_challenge':'challenge'}
TOKEN = {'access_token':'PRIVATE_ACCESS','refresh_token':'PRIVATE_REFRESH','expires_in':3600,
         'id_token':'e30.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoid29ya3NwYWNlIn19.sig'}


class DeviceOAuthTests(unittest.TestCase):
    def setUp(self):
        self.authorized = True
        self.identity = 'merchant-a'
        self.app = SimpleNamespace(state_lock=threading.RLock(),
            store=SimpleNamespace(settings={'router':'https://router.example'}, enrollment=lambda:{'id':self.identity}),
            platform_client=SimpleNamespace(lock=threading.RLock(), authorized=lambda session,identity:self.authorized),
            remote={'pools':[{'name':'gpt','provider':'openai'}]})
        self.oauth = OAuthService(self.app)
        self.addCleanup(self.wait_jobs)

    def call(self, action, body=None, session='browser-a', identity='merchant-a'):
        with self.app.state_lock, self.app.platform_client.lock:
            return self.oauth.dispatch(action, body or {}, identity, session)

    def start(self, **options):
        with patch('oauth_service.request', return_value=DEVICE) as network:
            result = self.call('start', {'entry':{'pool':'gpt','proxy':'socks5h://proxy.example:1080'},
                'save_refresh':True, **options})
        network.assert_called_once_with('openai', PROVIDERS['openai']['device_code'], 'socks5h://proxy.example:1080',
            json_body={'client_id':PROVIDERS['openai']['client_id']})
        return result, self.oauth.flows[result['id']]

    def wait_jobs(self):
        deadline=time.monotonic()+3
        while self.oauth.device_jobs and time.monotonic()<deadline: time.sleep(.005)
        self.assertFalse(self.oauth.device_jobs, 'device worker did not finish')

    def test_pending_is_throttled_and_approval_is_one_time_proxy_bound_and_draft_only(self):
        state, flow = self.start()
        self.assertEqual(state['auth_url'], 'https://auth.openai.com/codex/device')
        self.assertEqual(state['user_code'], 'ABCD-EFGH')
        self.assertFalse(state['manual_callback'])
        self.assertNotIn('PRIVATE',json.dumps(state))
        self.assertAlmostEqual(flow['expiry']-time.monotonic(),900,delta=2)
        with patch('oauth_service.request', side_effect=[OAuthError('pending',status=403),APPROVED,TOKEN]) as network:
            self.call('status', {'id':flow['id']});self.wait_jobs()
            self.assertEqual(flow['status'],'waiting')
            self.assertGreaterEqual(flow['next_poll']-time.monotonic(),4)
            for _ in range(10): self.call('status', {'id':flow['id']})
            self.assertEqual(network.call_count,1)
            flow['next_poll']=0
            self.call('status', {'id':flow['id']});self.wait_jobs()
            self.assertEqual(flow['status'],'ready')
            for _ in range(3):
                self.assertNotIn('PRIVATE',json.dumps(self.call('status', {'id':flow['id']})))
            self.assertEqual(network.call_count,3)
            self.assertTrue(all(c.args[2]=='socks5h://proxy.example:1080' for c in network.call_args_list))
            exchange=network.call_args.kwargs['data']
            self.assertEqual(exchange['redirect_uri'],'https://auth.openai.com/deviceauth/callback')
            self.assertEqual(exchange['code_verifier'],'PRIVATE_VERIFIER')
            self.assertEqual(exchange['code'],'PRIVATE_CODE')
        self.assertNotIn('device_auth_id',flow)
        self.assertNotIn('proxy',flow)
        value=self.call('result',{'id':flow['id']})
        self.assertEqual(value['credential']['account_id'],'workspace')
        self.assertEqual(value['credential']['refresh_token'],'PRIVATE_REFRESH')
        self.assertNotIn('credential',flow)
        with self.assertRaises(ValueError):self.call('result',{'id':flow['id']})

    def test_device_flow_rejects_callback_and_other_browser_or_merchant(self):
        _,flow=self.start()
        with self.assertRaises(ValueError):self.call('submit',{'id':flow['id'],'code':'http://localhost:1455/auth/callback?code=test'})
        for action in ('status','open','result','cancel'):
            with self.assertRaises(ValueError): self.call(action,{'id':flow['id']},session='browser-b')
            with self.assertRaises(ValueError): self.call(action,{'id':flow['id']},identity='merchant-b')
        self.assertEqual(flow['status'],'waiting')

    def test_cancel_during_poll_does_not_exchange_and_does_not_block_service(self):
        _,flow=self.start();entered=threading.Event();release=threading.Event()
        def network(*args,**kwargs):
            entered.set();release.wait(3);return APPROVED
        with patch('oauth_service.request',side_effect=network) as network_call:
            try:
                self.call('status',{'id':flow['id']})
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.call('cancel',{'id':flow['id']})['cancelled'])
                self.assertNotIn('device_auth_id',flow)
            finally:release.set();self.wait_jobs()
            self.assertEqual(network_call.call_count,1)
        self.assertNotIn('credential',flow)

    def test_cancellation_during_exchange_cannot_publish_tokens(self):
        _,flow=self.start();entered=threading.Event();release=threading.Event()
        def network(provider,url,proxy,**kwargs):
            if url.endswith('/deviceauth/token'):return APPROVED
            entered.set();release.wait(3);return TOKEN
        with patch('oauth_service.request',side_effect=network):
            try:
                self.call('status',{'id':flow['id']});self.assertTrue(entered.wait(1))
                self.call('cancel',{'id':flow['id']})
            finally:release.set();self.wait_jobs()
        self.assertNotIn('credential',flow)

    def test_expiry_clears_secrets_and_allows_new_attempt(self):
        _,flow=self.start();flow['expiry']=time.monotonic()-1
        with patch('oauth_service.request') as network:
            self.assertEqual(self.call('status',{'id':flow['id']})['status'],'expired')
            network.assert_not_called()
        self.assertNotIn('device_auth_id',flow)
        self.start()

    def test_logout_or_configuration_change_while_polling_discards_result(self):
        for change in ('logout','merchant','router','pool','expiry'):
            with self.subTest(change=change):
                self.setUp();_,flow=self.start()
                def network(*args,**kwargs):
                    if change=='logout':self.authorized=False
                    elif change=='merchant':self.identity='merchant-b'
                    elif change=='router':self.app.store.settings['router']='https://other.example'
                    elif change=='pool':self.app.remote['pools']=[]
                    else:flow['expiry']=0
                    return APPROVED
                with patch('oauth_service.request',side_effect=network) as call:
                    self.call('status',{'id':flow['id']});self.wait_jobs()
                    self.assertEqual(call.call_count,1)
                self.assertIn(flow['status'],('failed','expired'))
                self.assertNotIn('device_auth_id',flow)

    def test_poll_failures_are_not_mistaken_for_pending_and_exchange_is_not_replayed(self):
        for responses in ([OAuthError('pending',status=404)], [OAuthError('error',status=429)],
                          [OAuthError('network')], [APPROVED,OAuthError('uncertain',uncertain=True,status=503)],
                          [{'authorization_code':'incomplete'}]):
            with self.subTest(responses=responses):
                self.setUp();_,flow=self.start()
                with patch('oauth_service.request',side_effect=responses) as network:
                    self.call('status',{'id':flow['id']});self.wait_jobs()
                    expected='waiting' if getattr(responses[0],'status',None)==404 else 'failed'
                    self.assertEqual(flow['status'],expected)
                    self.call('status',{'id':flow['id']})
                    self.assertEqual(network.call_count,len(responses))
                if expected=='failed':self.start()

    def test_refresh_is_optional_and_device_response_is_validated(self):
        _,flow=self.start(save_refresh=False)
        with patch('oauth_service.request',side_effect=[APPROVED,TOKEN]):
            self.call('status',{'id':flow['id']});self.wait_jobs()
        self.assertNotIn('refresh_token',self.call('result',{'id':flow['id']})['credential'])
        for response in ({}, {**DEVICE,'interval':True}, {**DEVICE,'user_code':'bad\ncode'}):
            with patch('oauth_service.request',return_value=response),self.assertRaises(OAuthError):
                self.call('start',{'entry':{'pool':'gpt','proxy':'http://proxy.example'},'save_refresh':True})
        self.assertFalse(self.oauth.flows)

    def test_http_status_is_available_without_disclosing_provider_error_body(self):
        for status in (403,404,429,500):
            transport=httpx.MockTransport(lambda req:httpx.Response(status,text='PRIVATE_ERROR_BODY'))
            client=httpx.Client(transport=transport)
            with patch('oauth_network.httpx.Client',return_value=client):
                with self.assertRaises(OAuthError) as raised:
                    request('openai',PROVIDERS['openai']['device_poll'],'http://proxy.example',json_body={})
                self.assertEqual(raised.exception.status,status)
                self.assertNotIn('PRIVATE',str(raised.exception))


if __name__=='__main__':unittest.main()
