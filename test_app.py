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
    def test_archive_precedes_ack_and_replay_is_idempotent(self):
        t=Transport();app=Application(self.store,t);app.sync()
        self.assertEqual(len(self.store.records('usage')),1);self.assertEqual(t.acks[0]['through_seq'],1)
        self.store.archive('usage','instance',[t.row]);self.assertEqual(len(self.store.records('usage')),1)
        self.assertNotIn(b'REFUSAL_ARCHIVE_TEXT',(Path(self.tmp.name)/'records.db').read_bytes())
        altered={**t.row,'event':{'changed':True}}
        with self.assertRaises(RuntimeError):self.store.archive('usage','instance',[altered])
    def test_failed_disk_write_never_acknowledges(self):
        t=Transport();app=Application(self.store,t)
        def broken(*args):raise OSError('disk full')
        self.store.archive=broken
        with self.assertRaises(OSError):app.sync()
        self.assertEqual(t.acks,[])
    def test_rollback_rejected_and_wrong_identity_rejected(self):
        app=Application(self.store,Transport());identity=self.store.enrollment()['id']
        app.accept({'merchant':{'id':identity},'settings':{'revision':5}})
        with self.assertRaises(RuntimeError):app.accept({'merchant':{'id':identity},'settings':{'revision':4}})
        with self.assertRaises(RuntimeError):app.accept({'merchant':{'id':'other'},'settings':{'revision':6}})
    def test_client_cannot_change_local_private_key_through_settings_api(self):
        app=Application(self.store,Transport())
        with self.assertRaises(ValueError):app.api('PUT','/api/settings',{'token':'overwrite'})

if __name__=='__main__':unittest.main()
