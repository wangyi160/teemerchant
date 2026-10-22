import json
from pathlib import Path
import tempfile
import unittest
from app import Application
from store import Store

class Transport:
    def __init__(self):
        self.acks=[]
        self.row={'seq':1,'event':{'user_id':'alice','model':'model','secret_marker':'REFUSAL_ARCHIVE_TEXT'}}
    def close(self): pass
    def call(self,method,path,body=None):
        if method=='GET':
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
        self.assertEqual(self.store.enrollment()['id'],'m_'+self.store.enrollment()['credential_hash'][:32])
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
        t=Transport();app=Application(self.store,t)
        body={'pool':'official','proxy':'https://proxy.example','credential':{'access_token':'PRIVATE_PROBE'}}
        with self.assertRaises(ValueError):app._api('POST','/api/accounts/test',body)
        self.assertEqual(t.acks,[])
        app.connected=True
        before=self.store.path.read_bytes()
        app._api('POST','/api/accounts/test',body)
        self.assertEqual(t.acks,[body])
        self.assertEqual(self.store.path.read_bytes(),before)
        for invalid in [{**body,'proxy':''},{**body,'base_url':'https://other.example'},
                        {**body,'api_key':'PRIVATE_KEY'}]:
            with self.assertRaises(ValueError):app._api('POST','/api/accounts/test',invalid)
        self.assertEqual(len(t.acks),1)
if __name__=='__main__':unittest.main()
