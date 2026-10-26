"""Exercise the real platform serialization and the separate credential channel."""
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import httpx
from app import Application
from credential_protocol import public_digest
from local_accounts import config_from
from store import Store
from test_browser import FakeRouter


class PlatformControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.settings.update(router='https://fixture.example',platform_url='https://platform.example',credentials_saved=True)
        self.store.save();self.app=Application(self.store)
        self.state=FakeRouter(self.store).remote
        self.state['merchant'].update(runtime_id='test-runtime',control_protocol='platform-config-v1')
        self.app.accept(copy.deepcopy(self.state),clean_catalog=False)
        self.client=httpx.Client
    def tearDown(self):
        self.store.db.close();self.temp.cleanup()
    def http(self, callback):
        return patch('platform_data.httpx.Client',side_effect=lambda **kw:self.client(**kw,transport=httpx.MockTransport(callback)))

    def test_queries_never_contact_router_and_do_not_wait_for_credential_io(self):
        self.app.transport.call=Mock(side_effect=AssertionError('Router query'))
        self.app.state_lock.acquire()
        result=[]
        try:
            with self.http(lambda r:httpx.Response(200,json=self.state)):
                thread=threading.Thread(target=lambda:result.append(self.app.api('GET','/api/merchant',{})))
                thread.start();thread.join(2)
                self.assertFalse(thread.is_alive(),'page waited for TEE mutation lock')
        finally:self.app.state_lock.release()
        self.assertEqual(result[0]['merchant']['id'],self.store.enrollment()['id'])
        self.app.transport.call.assert_not_called()

    def test_accounts_activate_directly_even_if_platform_is_offline(self):
        update=config_from(self.state)
        update['accounts']={'credential-ref':{'provider':'openai','enabled':False,'proxy':'https://user:PRIVATE_PROXY@proxy.example',
            'credential':{'access_token':'PRIVATE_ACCESS','refresh_token':'PRIVATE_REFRESH','account_id':'PRIVATE_WORKSPACE'}}}
        calls=[]
        def tee(method,path,body):
            self.assertEqual((method,path),('PUT','/merchant/accounts/sync'))
            self.assertNotIn('PRIVATE_REFRESH',json.dumps(body))
            self.assertIn('PRIVATE_ACCESS',json.dumps(body));self.assertIn('PRIVATE_PROXY',json.dumps(body))
            self.assertFalse(body['accounts']['credential-ref']['enabled'])
            self.assertNotIn('ticket',body)
            calls.append(body)
            return copy.deepcopy(self.state)
        self.app.transport.call=tee
        with self.http(lambda _: (_ for _ in ()).throw(AssertionError('account save contacted platform'))):
            self.assertEqual(self.app.commit_accounts(update),self.state)
        self.app.platform_data.notify_synced=Mock(side_effect=RuntimeError('platform offline'))
        self.app.notify_snapshot()
        self.assertIsNotNone(self.app.snapshot_notice)
        self.assertTrue(self.app.connected)
        self.assertEqual(len(calls),1)
        self.assertNotIn('PRIVATE',json.dumps(self.app.snapshot_notice[1]))

    def test_price_only_uses_platform_and_rejection_keeps_confirmed_value(self):
        self.app.transport.call=Mock(side_effect=AssertionError('price used TEE channel'))
        view=self.app.remote
        update={'pool':'openai','multiplier_millionths':50000,'expected_revision':7,'_local_revision':view['local_revision']}
        def platform(request):
            if request.method=='GET':return httpx.Response(200,json=self.state)
            value=json.loads(request.content)
            self.assertNotIn('ticket',value);self.assertNotIn('accounts',value['config'])
            return httpx.Response(200,json={'status':'rejected','operation_id':request.headers['Idempotency-Key'],'error':'revision_conflict'})
        with self.http(platform),self.assertRaisesRegex(RuntimeError,'保留上次确认值'):
            self.app.save_pool_price(update)
        self.assertEqual(self.app.remote['settings']['pool_pricing'][0]['multiplier_millionths'],200000)
        self.assertIsNone(self.store.accounts.pending(self.store.settings['router']))
        self.app.transport.call.assert_not_called()

    def test_lost_platform_reply_recovers_by_operation_without_replaying(self):
        calls=[];operation=[]
        def platform(request):
            calls.append(request.method)
            if request.method=='PUT':
                operation.append(request.headers['Idempotency-Key']);raise httpx.ReadTimeout('lost')
            self.assertTrue(request.url.path.endswith('/commands/'+operation[0]))
            return httpx.Response(200,json={'status':'applied','operation_id':operation[0],'state':self.state})
        with self.http(platform):self.app.commit_config(config_from(self.state))
        self.assertEqual(calls,['PUT','GET'])

    def test_old_snapshot_cannot_revert_a_confirmed_price(self):
        newer=copy.deepcopy(self.state);newer['settings']['revision']=8
        newer['settings']['pool_pricing'][0]['multiplier_millionths']=50000
        self.app.accept(newer,clean_catalog=False)
        with self.http(lambda _:httpx.Response(200,json=self.state)):
            view=self.app.api('GET','/api/merchant',{})
        self.assertEqual(view['settings']['revision'],8)
        self.assertEqual(view['settings']['pool_pricing'][0]['multiplier_millionths'],50000)

    def test_router_restart_is_learned_from_platform_and_reconnected_in_background(self):
        self.state['platform_snapshot']={'router_locked':True}
        self.app.transport.call=Mock(side_effect=AssertionError('page reached Router'))
        with self.http(lambda _:httpx.Response(200,json=self.state)):
            self.app.api('GET','/api/merchant',{})
        self.assertFalse(self.app.connected)
        self.app.transport.call.assert_not_called()
        stop=Mock();stop.wait.side_effect=[False,True]
        with patch.object(self.app,'connect') as reconnect:
            self.app.background(stop)
        reconnect.assert_called_once()
