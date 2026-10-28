import json
from pathlib import Path
import tempfile
import unittest
from app import Application
from store import Store

class Transport:
    def __init__(self):
        self.acks=[]
        self.pools=[]
        self.calls=[]
        self.row={'seq':1,'event':{'user_id':'alice','model':'model','secret_marker':'REFUSAL_ARCHIVE_TEXT'}}
    def close(self): pass
    def call(self,method,path,body=None):
        self.calls.append((method,path,body))
        if method=='GET':
            if path=='/merchant/state':return {'pools':self.pools}
            if path.startswith('/merchant/usage'):
                return {'instance_id':'instance','data':[] if self.acks else [self.row]}
            return {'instance_id':'instance','data':[]}
        self.acks.append(body)
        return {}

class MerchantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def test_identity_persists_encrypted_without_public_secret(self):
        token=self.store.settings['token']
        self.assertNotIn(token.encode(),self.store.path.read_bytes())
        self.assertNotIn('token',self.store.public())
        self.assertEqual(self.store.enrollment()['id'],'')
        other=Store(self.tmp.name);self.assertEqual(other.settings['token'],token);other.db.close()
    def test_legacy_archive_remains_encrypted_and_replay_is_idempotent(self):
        t=Transport()
        self.store.archive('usage','instance',[t.row])
        self.store.archive('usage','instance',[t.row]);self.assertEqual(len(self.store.records('usage')),1)
        self.assertNotIn(b'REFUSAL_ARCHIVE_TEXT',(Path(self.tmp.name)/'records.db').read_bytes())
        altered={**t.row,'event':{'changed':True}}
        with self.assertRaises(RuntimeError):self.store.archive('usage','instance',[altered])
    def test_rollback_rejected_and_wrong_identity_rejected(self):
        app=Application(self.store,Transport());identity=self.store.enrollment()['id']
        app.accept({'merchant':{'id':identity},'settings':{'revision':5}})
        with self.assertRaises(RuntimeError):app.accept({'merchant':{'id':identity},'settings':{'revision':4}})
        with self.assertRaises(RuntimeError):app.accept({'merchant':{'id':'other'},'settings':{'revision':6}})
    def test_client_cannot_change_local_private_key_through_settings_api(self):
        app=Application(self.store,Transport())
        with self.assertRaises(ValueError):app.api('PUT','/api/settings',{'token':'overwrite'})
    def test_probe_forwards_only_after_connection_and_does_not_save_accounts(self):
        t=Transport();app=Application(self.store,t);app.platform_state=lambda: {'pools':t.pools}
        body={'pool':'official','proxy':'https://proxy.example','credential':{'access_token':'PRIVATE_PROBE'}}
        with self.assertRaises(ValueError):app._api('POST','/api/accounts/test',body)
        self.assertEqual(t.acks,[])
        app.connected=True
        t.pools=[{'name':'official','provider':'openai','deployments':[{'id':'a'}]}]
        app.remote={'pools':t.pools}
        before=self.store.path.read_bytes()
        app._api('POST','/api/accounts/test',body)
        self.assertEqual(t.acks,[body])
        self.assertEqual(self.store.path.read_bytes(),before)
        for invalid in [{**body,'proxy':''},{**body,'base_url':'https://other.example'},
                        {**body,'api_key':'PRIVATE_KEY'}]:
            with self.assertRaises(ValueError):app._api('POST','/api/accounts/test',invalid)
        self.assertEqual(len(t.acks),1)

    def test_probe_uses_published_pools_not_local_account_count(self):
        for local_count,published in [(1,False),(0,False),(1,True),(0,True)]:
            with self.subTest(local_count=local_count,published=published):
                t=Transport();app=Application(self.store,t);app.platform_state=lambda: {'pools':t.pools};app.connected=True
                pool={'name':'openai','provider':'openai','allowed_models':['model-a'],
                      'deployments':[{'id':'paused-account','enabled':False}]*local_count}
                app.remote={'pools':[pool]}
                if published:t.pools=[{'name':'openai','deployments':[{'id':'remote-account'}]}]
                body={'pool':'openai','proxy':'https://proxy.example','model':'model-a',
                      'credential':{'access_token':'FRESH_TOKEN','refresh_token':'LOCAL_ONLY'}}
                before=self.store.path.read_bytes()
                app._api('POST','/api/accounts/test',body)
                self.assertEqual([(m,p) for m,p,_ in t.calls],
                                 [('POST','/merchant/accounts/test')])
                sent=t.acks[0]
                self.assertEqual(sent['credential'],{'access_token':'FRESH_TOKEN'})
                self.assertEqual(sent['proxy'],body['proxy'])
                self.assertEqual(sent['model'],'model-a')
                if published:self.assertNotIn('draft_pool',sent)
                else:self.assertEqual(sent['draft_pool'],{'provider':'openai','allowed_models':['model-a']})
                self.assertEqual(self.store.path.read_bytes(),before)
                self.assertFalse(self.store.accounts.path.exists())
                self.assertIn('refresh_token',body['credential'])

    def test_probe_does_not_send_credentials_if_pool_state_cannot_be_read(self):
        from unittest.mock import Mock
        t=Transport();app=Application(self.store,t);app.platform_state=lambda: {'pools':t.pools};app.connected=True
        app.remote={'pools':[{'name':'openai','provider':'openai','deployments':[]}]}
        body={'pool':'openai','proxy':'https://proxy.example','credential':{'access_token':'PRIVATE'}}
        for result in (None,{'data':[]}):
            app.platform_state=Mock(return_value=result);t.call=Mock()
            with self.assertRaises(RuntimeError):app._api('POST','/api/accounts/test',body)
            t.call.assert_not_called();app.platform_state.assert_called_once_with()
        app.platform_state=Mock(side_effect=RuntimeError('unavailable'));t.call=Mock()
        with self.assertRaises(RuntimeError):app._api('POST','/api/accounts/test',body)
        t.call.assert_not_called();app.platform_state.assert_called_once_with()
if __name__=='__main__':unittest.main()
