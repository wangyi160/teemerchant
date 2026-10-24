import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
from app import Application
from store import Store
from test_platform_client import login_fixture


def withdrawal(state='pending'):
    return {'id':'1','state':state,'version':1,'events':[],'amount_microusd':'1000000','fee_microusd':'10000',
            'payout_microusd':'990000','fee_rate_bps':100,'network':'tron','network_name':'TRON','asset':'USDT',
            'address':'fixture-address','created_at':1792800000}


def withdrawals(state='pending'):
    return {'data':[withdrawal(state)],'has_more':False,'next_before':None,'can_apply':True,
            'channels':[{'id':'tron','name':'TRON','asset':'USDT'}],
            'balance':{k:'1000000' for k in ('available_microusd','reserved_microusd','paid_microusd','paid_payout_microusd','paid_fee_microusd')},
            'fee_policy':{'version':1,'minimum_microusd':'1','tiers':[{'from_microusd':'1','to_microusd':None,'rate_bps':100}]}}


class PlatformDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.settings.update(platform_url='https://platform.example',credentials_saved=True)
        self.store.save();self.app=Application(self.store)
        self.client=httpx.Client

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def mock(self,handler):
        return patch('platform_data.httpx.Client',side_effect=lambda **kw:self.client(**kw,transport=httpx.MockTransport(handler)))

    def test_live_queries_do_not_write_local_records_and_use_only_platform_secret(self):
        queries=[];writes=[]
        owner=self.store.control_token().split('.')[1]
        self.store.db.set_trace_callback(lambda q:writes.append(q) if q.split()[0] in ('INSERT','UPDATE','DELETE') else None)
        def fetch(request):
            queries.append(str(request.url))
            self.assertEqual(request.headers['Authorization'],'Bearer '+self.store.settings['token'])
            self.assertNotIn(owner,str(request.headers))
            return httpx.Response(200,json={'data':[{'seq':'31','event':{'model':'fixture'}}],'has_more':False})
        with self.mock(fetch),patch.object(self.app.transport,'call',side_effect=AssertionError('No Router requests')):
            value=self.app.api('GET','/api/records/refusals?before=40',{})
            self.app.api('GET','/api/revenue?pool=example',{})
            self.app.api('GET','/api/revenue/record?instance=platform&seq=31',{})
        self.assertEqual(value['data'][0]['seq'],'31')
        self.assertEqual(queries[0],'https://platform.example/merchant-api/v1/activity?kind=refusals&limit=50&before=40')
        self.assertTrue(queries[1].endswith('/revenue?pool=example'))
        self.assertTrue(queries[2].endswith('/activity/31'));self.assertEqual(writes,[])
        self.assertFalse(hasattr(self.app,'archive_background'))
        for path in ('/api/sync','/api/history/sync'):
            with self.assertRaises(ValueError):self.app.api('POST',path,{})

    def test_withdrawals_and_receipts_are_live_authorized_reads(self):
        identity=self.store.enrollment()['id']
        with self.mock(lambda _:self.fail('Logged-out query')):
            with self.assertRaises(PermissionError):self.app.platform_client.call('browser','merchant','GET','withdrawals',{},'',identity)
        login_fixture(self.app,'browser')
        paths=[]
        with self.mock(lambda r:(paths.append(r.url.path),httpx.Response(200,json={'data':[]}))[1]):
            for path in ('withdrawals','withdrawals/1','receipts/mine','receipts/records/1'):
                self.assertEqual(self.app.platform_client.call('browser','merchant','GET',path,{},'',identity).status,200)
        self.assertEqual(len(paths),4)

    def test_failed_queries_do_not_return_persisted_history_or_follow_redirects(self):
        self.store.archive('usage','old',[{'seq':1,'event':{'model':'STALE_DATA'}}])
        for code in (302,401,500):
            with self.mock(lambda _:httpx.Response(code,headers={'Location':'https://other.example'},json={'data':[]})):
                with self.assertRaisesRegex(RuntimeError,'HTTP '+str(code)):
                    self.app.api('GET','/api/records/usage',{})
        with self.mock(lambda _:(_ for _ in ()).throw(httpx.ReadTimeout('offline'))):
            with self.assertRaises(RuntimeError):self.app.api('GET','/api/records/usage',{})

    def test_inflight_result_is_discarded_after_profile_reset_or_logout(self):
        def changed(request):
            self.app.platform_data.reset()
            return httpx.Response(200,json={'data':[{'private':'OLD_ACCOUNT'}]})
        with self.mock(changed):
            with self.assertRaises(ValueError):self.app.api('GET','/api/records/usage',{})
        session=login_fixture(self.app,'browser');identity=self.store.enrollment()['id']
        def logout(request):
            self.app.platform_client.revoke('browser',session)
            return httpx.Response(200,json={'data':[{'private':'OLD_ACCOUNT'}]})
        with self.mock(logout):
            with self.assertRaises(PermissionError):self.app.platform_client.call('browser','merchant','GET','withdrawals',{},'',identity)


if __name__=='__main__':unittest.main()
