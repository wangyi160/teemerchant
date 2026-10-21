from datetime import datetime
from pathlib import Path
import tempfile
import unittest
import copy
from app import Application
from store import Store


def receipt(request='request-1', multiplier=200000, quantity=125, price=2_000_000, at=None):
    base = quantity * price
    return {'seq':1,'event':{'event':'request','merchant_id':'m_test','merchant_price_version':7,
        'request_id':request,'user_id':'customer-1','pool':'grok','model':'model-a','provider':'xai',
        'status':200,'attempts':1,'created_at':at or int(datetime.now().timestamp()),
        'usage':{'input_tokens':quantity,'output_tokens':0,'unknown_usage_requests':0},
        'merchant_pricing':{'basis':'official_pool_multiplier','model':'model-a','pool':'grok',
            'multiplier_millionths':multiplier,'official_price_revision':3,'official_version':2,
            'official_checked_at':1,'official_source':'https://example.test/prices',
            'input':price,'output':6_000_000,'cache_read':500000,'long_context':None},
        'billing':{'version':1,'state':'calculated','calculator':'grok_v1','provider':'xai',
            'basis':'official_list_price','currency':'USD','official_price_revision':3,'official_version':2,
            'multiplier_millionths':multiplier,'official_cost_picousd':str(base),
            'charge_numerator':str(base*multiplier),'charge_denominator':'1000000000000',
            'rounding':'ceil_once_to_micro_usd','charge_microusd':(base*multiplier+10**12-1)//10**12,
            'official_cost_microusd':(base+10**6-1)//10**6,
            'items':[{'kind':'input','quantity':quantity,'unit':'million_tokens',
                'unit_price_microusd':price,'amount_picousd':str(base)}]}}}


class RevenueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_v2_estimates_require_failed_attempt_and_are_visible_in_revenue(self):
        row=receipt('error-estimate');e=row['event'];b=e['billing']
        b.update(usage_source='mixed',coverage='partial',upstream_status=200,
            usage={'input':125,'output':0},reported_usage={'input':125},estimate={
                'method':'o200k_json_text_v1','input_tokens':125,'visible_output_tokens':0,
                'input_source':'upstream','output_source':'local','cache_policy':'ordinary_input',
                'context_incomplete':False,'hidden_output_included':False})
        e['status']=499
        e['billing']={k:copy.deepcopy(v) for k,v in b.items()
            if k not in ('usage','items','reported_usage','estimate','upstream_status')}
        e['billing'].update(version=2,unknown_attempts=0,
            attempt_records=[{'number':1,'status':499,'billing':b}])
        from revenue import router_charge
        self.assertEqual(router_charge(e),(50,250))
        e['billing']['attempt_records'][0]['status']=200
        self.assertIsNone(router_charge(e))
        e['billing']['attempt_records'][0]['status']=499
        self.store.archive('usage','boot',[row])
        result=self.store.revenue()
        self.assertEqual(result['summary']['charge_microusd'],'50')
        self.assertEqual(result['data'][0]['usage_source'],'mixed')

    def test_all_history_pagination_exact_money_and_restart(self):
        rows=[]
        for i in range(251):
            r=receipt('request-'+str(i));r['seq']=i+1;rows.append(r)
        self.store.archive('usage','boot-1',rows)
        self.assertEqual(len(self.store.records('usage')),200)
        r=self.store.revenue()
        self.assertEqual(r['summary']['requests'],251)
        self.assertEqual(r['summary']['charge_microusd'],str(50*251))
        self.assertEqual(len(r['data']),50)
        self.assertTrue(r['has_more'])
        self.assertEqual(len(self.store.revenue('offset=250')['data']),1)
        self.assertFalse(self.store.revenue('offset=250')['has_more'])
        other=Store(self.tmp.name)
        try:self.assertEqual(other.revenue()['summary'],r['summary'])
        finally:other.db.close()
        self.assertNotIn(b'customer-1',(Path(self.tmp.name)/'records.db').read_bytes())

    def test_deduplicate_replay_and_recovered_request_across_boots(self):
        row=receipt()
        self.store.archive('usage','boot-1',[row]);self.store.archive('usage','boot-1',[row])
        self.store.archive('usage','boot-2',[row])
        self.assertEqual(self.store.revenue()['summary']['charge_microusd'],'50')
        self.assertEqual(self.store.revenue()['summary']['requests'],1)
        conflicting=receipt(multiplier=800000)
        self.store.archive('usage','boot-3',[conflicting])
        r=self.store.revenue()
        self.assertEqual(r['summary']['charge_microusd'],'0')
        self.assertEqual(r['summary']['pending'],1)
        self.assertEqual(r['data'][0]['reason'],'conflicting_request_records')

    def test_pending_invalid_and_failed_receipts_never_fall_back_to_legacy(self):
        mutations=[lambda e:e['billing'].update(state='pending',reason='request_incomplete'),
                   lambda e:e['billing'].update(charge_microusd=999),
                   lambda e:e['billing']['items'][0].update(quantity=126),
                   lambda e:e['billing'].update(multiplier_millionths=True),
                   lambda e:e.update(status=499),lambda e:e.update(attempts=2)]
        for i,mutate in enumerate(mutations):
            row=receipt('bad-'+str(i));row['seq']=i+1;mutate(row['event'])
            self.store.archive('usage','boot',[row])
        r=self.store.revenue()
        self.assertEqual(r['summary']['pending'],len(mutations))
        self.assertEqual(r['summary']['charge_microusd'],'0')
        self.assertTrue(all(row['charge_microusd'] is None for row in r['data']))

    def test_legacy_uses_historic_cache_and_context_rates(self):
        row=receipt();event=row['event'];del event['billing']
        event['usage'].update(input_tokens=150,output_tokens=25)
        event['cache_usage']={'read':50,'write':0,'write_hour':0}
        event['merchant_pricing']['long_context']={'above_input_tokens':100,
            'rates':{'input':4_000_000,'output':12_000_000,'cache_read':1_000_000}}
        self.store.archive('usage','boot',[row])
        r=self.store.revenue()
        self.assertEqual(r['data'][0]['charge_microusd'],'150')
        self.assertEqual(r['data'][0]['basis'],'legacy_quote')

    def test_large_exact_amounts_and_zero_rate(self):
        for multiplier in [0,10**15]:
            row=receipt(str(multiplier),multiplier=multiplier,quantity=10**8)
            row['seq']=multiplier+1
            self.store.archive('usage','boot',[row])
        r=self.store.revenue()
        self.assertEqual(r['summary']['charge_microusd'],'200000000000000000')
        self.assertEqual(r['summary']['calculated'],2)
        detail=self.store.revenue_record('boot',10**15+1)
        self.assertEqual(detail['event']['billing']['charge_microusd'],'200000000000000000')

    def test_dates_pool_filters_and_original_record(self):
        a=receipt('old',at=int(datetime(2025,1,31,23,59,59).timestamp()))
        b=receipt('new',at=int(datetime(2025,2,1,0,0,0).timestamp()));b['seq']=2;b['event']['pool']='other'
        self.store.archive('usage','boot',[a,b])
        app=Application(self.store)
        report=app.api('GET','/api/revenue?start=2025-01-31&end=2025-01-31&pool=grok',{})
        self.assertEqual(report['filtered']['requests'],1)
        self.assertEqual(report['data'][0]['request_id'],'old')
        detail=app.api('GET','/api/revenue/record?instance=boot&seq=1',{})
        self.assertEqual(detail['event']['billing']['charge_microusd'],50)
        calendar=self.store.revenue_index.report(now=datetime(2025,2,1,12))
        self.assertEqual(calendar['summary']['charge_microusd'],'100')
        self.assertEqual(calendar['summary']['today_microusd'],'50')
        self.assertEqual(calendar['summary']['month_microusd'],'50')
        for query in ['start=bad','start=2025-02-01&end=2025-01-01','offset=-1','pool=a&pool=b','extra=1']:
            with self.assertRaises(ValueError):self.store.revenue(query)


if __name__=='__main__':unittest.main()
