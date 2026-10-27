import re
"""Optional UI integration tests: pip install playwright; Microsoft Edge required.

Uses an isolated local server and a fake TEE transport. Never touches live accounts.
"""
import copy
import hashlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import time
import threading
import unittest
from app import Application
from test_control_fixture import install
from service import LocalSessions, handler
from store import Store
from credential_protocol import access_only, PROTOCOL
try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


class FakeRouter:
    def __init__(self, store):
        self.store = store
        self.writes = []
        self.reject = False
        self.probes = []
        self.probe_result = {'ok':True,'model':'fixture-text','latency_ms':12}
        self.remote = {'merchant': {'id': store.enrollment()['id'], 'name': 'Example merchant', 'allow_test': True,
                                   'max_concurrent': 16, 'rpm': 0, 'tpm': 0},
            'settings': {'revision': 7, 'max_concurrent': 16, 'rpm': 0, 'tpm': 0, 'customers': [],
                         'customer_defaults': {'enabled': True, 'models': [], 'max_concurrent': 4, 'rpm': 0, 'tpm': 0},
                         'pool_pricing': [{'pool': 'openai', 'multiplier_millionths': 200000}]},
            'pools': [{'name': 'openai', 'provider': 'openai', 'deployments': [
                {'id': 'example-001', 'provider': 'openai', 'oauth_account': 'credential-ref', 'enabled': True,
                 'max_concurrent': 8, 'rpm': 30}]}],
            'accounts': {'data': [{'name': 'credential-ref', 'enabled': True, 'health': {'state': 'active'}}]},
            'catalog': {'data': []}, 'official_pricing': {'providers': []}}

    def close(self):
        pass

    def call(self, method, path, body=None):
        if path == '/merchant/bind':
            owner=hashlib.sha256(self.store.control_token().split('.')[1].encode()).hexdigest()
            self.remote['merchant'].update(owner_hash=owner,credential_protocol=PROTOCOL,runtime_id='browser-fixture')
            return {'merchant_id':self.store.enrollment()['id'],'owner_hash':owner,'merchant_control_version':1}
        if method == 'POST' and path == '/merchant/accounts/test':
            self.probes.append(copy.deepcopy(body))
            return copy.deepcopy(self.probe_result)
        if method == 'PUT':
            if self.reject:
                raise RuntimeError('Router 返回 409：revision_conflict')
            if body['expected_revision'] != self.remote['settings']['revision']:
                raise RuntimeError('stale revision')
            self.writes.append(copy.deepcopy(body))
            self.remote['pools'] = copy.deepcopy(body['pools'])
            for pool in self.remote['pools']:
                for account in pool['deployments']:
                    if account.get('proxy'):
                        account['proxy'] = '[redacted]'
            self.remote['settings']['pool_pricing'] = copy.deepcopy(body['pool_pricing'])
            if body.get('customer_defaults') is not None:
                self.remote['settings']['customer_defaults'] = copy.deepcopy(body['customer_defaults'])
            for customer in body.get('customers', []):
                self.remote['settings']['customers'] = [c for c in self.remote['settings']['customers'] if c['user_id'] != customer['user_id']] + [copy.deepcopy(customer)]
            self.remote['settings']['revision'] += 1
            for name, account in body.get('accounts', {}).items():
                previous = next((a for a in self.remote['accounts']['data'] if a['name'] == name), {'name': name})
                previous = {**previous, 'enabled': account.get('enabled', previous.get('enabled', True)), 'health': {'state': 'active'}}
                if 'credential' in account and 'api_key' not in account['credential']:
                    previous['access_sha256'] = hashlib.sha256(access_only(account['credential'])['access_token'].encode()).hexdigest()
                self.remote['accounts']['data'] = [a for a in self.remote['accounts']['data'] if a['name'] != name] + [previous]
        return copy.deepcopy(self.remote)


@unittest.skipUnless(sync_playwright, 'Optional Playwright dependency not installed')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        auth = patch('app.verify_account')
        auth.start(); self.addCleanup(auth.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.router = FakeRouter(self.store)
        self.app = install(Application(self.store, self.router), self.router)
        self.app.accept(copy.deepcopy(self.router.remote))
        self.platform_reads = []
        def platform_read(path):
            self.platform_reads.append(path)
            if path=='state':return copy.deepcopy(self.router.remote)
            if path=='withdrawals':
                from test_platform_data import withdrawals
                return withdrawals()
            if path.startswith('revenue'):
                from revenue import RevenueIndex
                return RevenueIndex().report('')
            if path.startswith('activity?') or path.startswith('receipts/mine'):
                return {'data': [], 'has_more': False, 'next_before': None}
            raise AssertionError(path)
        self.app.platform_data.fetch = platform_read
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.auth = LocalSessions()
        self.server.RequestHandlerClass = handler(self.app, self.origin, self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel='msedge', headless=True)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1100})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        from test_platform_client import login_fixture
        token = self.auth.create()
        login_fixture(self.app, token)
        # Page entry now revalidates identity. Fixture sessions must never be
        # sent to the real management platform during isolated browser tests.
        platform_request=self.app.platform_client.request
        def request(client,method,url,headers,data=None):
            if url=='https://teerouter.org/portal/api/me':
                session=next((s for s in self.app.platform_client.sessions.values()
                              if s.get('cookie')==headers.get('Cookie')),None)
                if not session:return 401,{'error':'login_required'},{}
                return 200,{**copy.deepcopy(session['view']),'csrf':session['csrf']},{}
            return platform_request(client,method,url,headers,data)
        self.app.platform_client.request=request
        self.page.context.add_cookies([{'name':'tm_session','value':token,'url':self.origin}])
        self.page.goto(self.origin)
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.get_by_role('heading', name='账号与模型', exact=True)).to_be_visible()

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def pass_probe(self):
        self.page.locator('#account-test').click()
        expect(self.page.locator('#account-test-status')).to_contain_text('通过')

    def discover_fixture_model(self):
        self.router.remote['catalog'] = {'data': [{'pool': 'openai', 'deployment': 'example-001',
            'official_models': {'fixture-discovered-model': {}},
            'catalog': {'fetched_at': int(time.time()), 'last_error': None, 'refreshing': False}}]}
        self.app.accept(copy.deepcopy(self.router.remote))

    def test_account_status_and_models_update_when_background_catalog_arrives(self):
        expect(self.page.get_by_text('等待模型目录', exact=True)).to_be_visible()
        self.discover_fixture_model()
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('目录已就绪', exact=True)).to_be_visible()
        expect(self.page.locator('#pool-model-catalog')).to_contain_text('fixture-discovered-model')
        self.assertEqual(self.page.evaluate('selectedAccountPool'), 'openai')
        self.page.evaluate("window.renderedCatalog=document.querySelector('#pool-model-catalog')")
        self.page.evaluate('pollAccountAccess()')
        self.assertTrue(self.page.evaluate("renderedCatalog===document.querySelector('#pool-model-catalog')"))
        self.router.remote['catalog']['data'][0]['catalog']['last_error'] = 'provider_unavailable'
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('目录获取失败', exact=True)).to_be_visible()

    def test_account_auth_failure_and_pause_override_shared_model_catalog(self):
        self.discover_fixture_model()
        book, router = self.store.accounts, self.store.settings['router']
        account = book.sync_accounts(router)['example-001']
        book.account_sync_result(router, 'example-001', account['basis'], 'paused',
                                 'authentication_failed', '账号认证失败（HTTP 401）')
        self.app.remote = book.view(router)
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('账号认证失败', exact=True)).to_be_visible()
        expect(self.page.get_by_text('目录已就绪', exact=True)).to_have_count(0)
        expect(self.page.get_by_text('等待模型目录', exact=True)).to_have_count(0)
        expect(self.page.get_by_text('账号认证失败（HTTP 401） · 自动同步已暂停', exact=True)).to_be_visible()
        expect(self.page.locator('[data-action=refresh-account][data-id=example-001]')).to_have_text('手动刷新')
        calls = []
        def retry(route):
            calls.append(route.request.post_data_json)
            book.account_sync_result(router, 'example-001', account['basis'], 'synced')
            self.app.remote = book.view(router)
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.app.remote))
        self.page.route('**/api/accounts/refresh', retry)
        self.page.locator('[data-action=refresh-account]').click()
        expect(self.page.get_by_text('目录已就绪', exact=True)).to_be_visible()
        self.assertEqual(calls, [{'id': 'example-001'}])

    def test_disabled_credential_cannot_appear_as_waiting_for_catalog(self):
        self.router.remote['accounts']['data'][0]['enabled'] = False
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('停用', exact=True)).to_be_visible()
        expect(self.page.get_by_text('等待模型目录', exact=True)).to_have_count(0)
        expect(self.page.locator('[data-action=toggle-account]')).to_have_text('启用')

    def test_renewal_recovery_shows_step_attempt_deadline_and_exhaustion(self):
        at = int(time.time()) + 60
        status = {'state':'retry','step':'upload','renewal_cycle':True,'failures':2,
                  'retry_at':at,'error':'Router 同步失败'}
        self.app.remote['account_sync'] = {'example-001':status}
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('等待自动重试', exact=True)).to_be_visible()
        expected = self.page.evaluate('at=>new Date(at*1000).toLocaleString("zh-CN",{hour12:false})',at)
        expect(self.page.get_by_text('Router 同步失败 · Token 同步第 2/4 次重试：'+expected,exact=True)).to_be_visible()
        expect(self.page.locator('[data-action=refresh-account]')).to_be_visible()
        status.update(state='paused',failures=5)
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('Router 同步失败 · 4 次重试均失败 · 自动同步已暂停',exact=True)).to_be_visible()
        expect(self.page.locator('[data-action=refresh-account]')).to_be_visible()

    def test_refresh_error_history_remains_visible_after_success_and_manual_button_is_available(self):
        at = int(time.time())
        rows = [
            {'at':at,'step':'refresh','code':'renewal_response_timeout','error':'续期响应超时，响应未完整到达',
             'attempt':0,'state':'retry','retry_at':at+30},
            {'at':at+30,'step':'refresh','code':'renewal_http_error','error':'续期接口返回错误（HTTP 400 · invalid_grant）',
             'attempt':1,'state':'retry','retry_at':at+90}]
        self.app.remote.update(account_sync={'example-001':{'state':'synced'}},
                               account_errors={'example-001':rows},account_can_refresh={'example-001':True})
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_role('button',name='手动刷新',exact=True)).to_be_visible()
        self.page.get_by_role('button',name='错误记录',exact=True).click()
        dialog=self.page.locator('#dialog')
        expect(dialog).to_contain_text('续期响应超时，响应未完整到达')
        expect(dialog).to_contain_text('HTTP 400 · invalid_grant')
        expect(dialog).to_contain_text('首次尝试')
        expect(dialog).to_contain_text('重试 1/4')
        expect(dialog).not_to_contain_text('需重新授权')
        self.page.get_by_role('button',name='关闭',exact=True).click()
        calls=[]
        def refresh(route):
            calls.append(route.request.post_data_json)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(self.app.remote))
        self.page.route('**/api/accounts/refresh',refresh)
        self.page.get_by_role('button',name='手动刷新',exact=True).click()
        expect(self.page.get_by_role('button',name='手动刷新',exact=True)).to_be_enabled()
        self.assertEqual(calls,[{'id':'example-001'}])

    def test_token_sync_timestamp_updates_in_place_and_remains_visible_on_failure(self):
        cell = self.page.locator('[data-token-sync=example-001]')
        expect(self.page.get_by_role('columnheader', name='最近 Access Token 同步')).to_be_visible()
        expect(cell).to_have_text('尚无记录')
        self.app.remote['access_token_sync'] = {'example-001': {'last_success_at': 1792836300, 'current': True}}
        self.page.evaluate('pollAccountAccess()')
        expected = self.page.evaluate("new Date(1792836300000).toLocaleString('zh-CN',{hour12:false})")
        expect(cell.locator('time')).to_have_text(expected)
        self.app.remote['account_sync'] = {'example-001': {'state': 'paused', 'code': 'connection_failed', 'error': '无法连接账号代理或官方服务'}}
        self.app.remote['access_token_sync']['example-001']['current'] = False
        self.page.evaluate('pollAccountAccess()')
        expect(cell.locator('time')).to_have_text(expected)
        expect(cell).to_contain_text('当前 Token 尚未同步')
        self.app.remote['access_token_sync']['example-001'] = {'last_success_at':1792836360, 'current':True}
        self.page.evaluate('pollAccountAccess()')
        expect(cell.locator('time')).not_to_have_text(expected)
        expect(cell).not_to_contain_text('当前 Token 尚未同步')

    def test_catalog_updates_wait_for_account_edit_to_close_without_losing_draft(self):
        self.page.locator('[data-action=edit-account]').click()
        self.page.locator('[name=max_concurrent]').fill('12')
        self.discover_fixture_model()
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('[name=max_concurrent]')).to_have_value('12')
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.locator('[data-action=close]').click()
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.get_by_text('目录已就绪', exact=True)).to_be_visible()
        self.assertEqual(self.router.writes, [])

    def test_catalog_updates_preserve_open_model_selection_editor(self):
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        self.page.locator('#pool-model-search').fill('unsaved-filter')
        self.discover_fixture_model()
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#pool-model-search')).to_have_value('unsaved-filter')
        self.page.locator('#pool-model-cancel').click()
        expect(self.page.locator('#pool-model-catalog')).to_contain_text('fixture-discovered-model')
        expect(self.page.get_by_text('目录已就绪', exact=True)).to_be_visible()
        self.assertEqual(self.router.writes, [])

    def test_refusal_policy_and_reset_are_user_scoped_and_preserve_other_limits(self):
        customer={'user_id':'existing-user','enabled':True,'models':[],'max_concurrent':3,
                  'rpm':7,'tpm':100,'refusal_limit':None,'refusal_baseline':2}
        self.router.remote['settings']['customers']=[customer]
        self.router.remote['customer_refusals']={'existing-user':{'total':6,'count':4,'limit':3,'blocked':True}}
        self.router.remote['settings']['customer_defaults']['refusal_limit']=3
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.reload()
        self.page.locator('aside [data-id=customers]').click()
        expect(self.page.get_by_text('拒答超限 · 自动停用',exact=True)).to_be_visible()
        self.page.locator('[data-action=customer-defaults]').click()
        self.page.locator('[name=refusal_limit]').fill('5')
        self.page.locator('#dialog button[type=submit]').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['customer_defaults']['refusal_limit'],5)
        self.page.locator('[data-action=customer]').click()
        self.page.locator('[name=refusal_limit]').fill('0')
        self.page.locator('#dialog button[type=submit]').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['customers'][0]['refusal_limit'],0)
        self.assertEqual(self.router.writes[-1]['customers'][0]['refusal_baseline'],2)
        self.page.locator('[data-action=reset-refusals]').click()
        self.page.wait_for_function("!document.querySelector('.toast') || document.querySelector('.toast').textContent !== '操作中'")
        for _ in range(50):
            if self.router.writes[-1].get('customers',[{}])[0].get('refusal_baseline')==6:break
            time.sleep(.02)
        reset=self.router.writes[-1]['customers'][0]
        self.assertEqual(reset['user_id'],'existing-user')
        self.assertEqual(reset['refusal_baseline'],6)
        self.assertEqual((reset['enabled'],reset['max_concurrent'],reset['rpm'],reset['tpm']),(True,3,7,100))

    def test_refusal_reset_stops_if_merchant_changes_during_refresh(self):
        result=self.page.evaluate('''async()=>{
            const oldApi=api,oldReload=reload,id=state.enrollment.id;let writes=0;
            try{
                api=async(path,method)=>{if(method==='PUT')writes++;return {}};
                reload=async()=>{state.enrollment.id='another-merchant'};
                try{await resetRefusals('u');return {stopped:false,writes}}
                catch(error){return {stopped:error.message.includes('商户已切换'),writes}}
            }finally{api=oldApi;reload=oldReload;state.enrollment.id=id}
        }''')
        self.assertEqual(result,{'stopped':True,'writes':0})

    def test_connection_required_and_invalidated_when_credentials_or_proxy_change(self):
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        self.page.locator('[name=id]').fill('checked-account')
        self.page.locator('[name=credential]').fill(json.dumps({'provider':'openai','credential':{'access_token':'PRIVATE_TOKEN'}}))
        save = self.page.get_by_role('button', name='保存并同步')
        expect(save).to_be_disabled()
        self.page.locator('[name=proxy]').fill('direct://')
        self.page.locator('#account-test').click()
        expect(self.page.locator('#account-test-status')).to_have_text('测试失败')
        self.assertEqual(self.router.probes, [])
        self.page.locator('[name=proxy]').fill('socks5h://proxy.example:1080')
        self.router.probe_result = {'ok':False,'code':'credential_rejected'}
        self.page.locator('#account-test').click()
        expect(self.page.locator('#dialog [role=alert]')).to_have_text('凭证被上游拒绝')
        expect(save).to_be_disabled()
        self.assertEqual(self.router.writes, [])
        self.router.probe_result = {'ok':True,'model':'fixture-text','latency_ms':12}
        self.pass_probe()
        expect(save).to_be_enabled()
        self.page.locator('[name=max_concurrent]').fill('4')
        expect(save).to_be_enabled()
        self.page.locator('[name=proxy]').fill('https://other.example')
        expect(save).to_be_disabled()
        self.pass_probe()
        self.page.locator('[name=credential]').fill(json.dumps({'provider':'openai','credential':{'access_token':'PRIVATE_NEW_TOKEN'}}))
        expect(save).to_be_disabled()
        self.pass_probe()
        self.assertEqual(self.router.probes[-1]['credential']['access_token'],'PRIVATE_NEW_TOKEN')
        self.assertEqual(self.router.probes[-1]['proxy'],'https://other.example')
        self.assertEqual(self.router.writes, [])
        save.click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes), 1)

    def test_account_save_preserves_config_changes_during_authorization_and_retries_stage_race(self):
        from local_accounts import config_from
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        self.page.locator('[name=id]').fill('new-account')
        self.page.locator('[name=credential]').fill(json.dumps({'provider':'openai','credential':{'access_token':'PRIVATE_NEW_TOKEN'}}))
        self.page.locator('[name=proxy]').fill('socks5h://proxy.example:1080')
        self.pass_probe()
        update = config_from(self.app.remote)
        update['pools'][0]['deployments'][0]['enabled'] = False
        update['pools'][0]['allowed_models'] = ['current-model']
        self.app.api('PUT', '/api/config', update)
        self.app.api('PUT', '/api/pool-pricing', {'pool': 'openai', 'multiplier_millionths': 400000,
            'expected_revision': self.app.remote['settings']['revision'], '_local_revision': self.app.remote['local_revision']})
        raced = []
        def race(route):
            if route.request.method == 'PUT' and not raced:
                raced.append(True)
                update = config_from(self.app.remote)
                update['pools'][0]['deployments'][0]['rpm'] = 27
                self.app.api('PUT', '/api/config', update)
            route.continue_()
        self.page.route('**/api/config', race)
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        result = self.router.writes[-1]
        self.assertFalse(result['pools'][0]['deployments'][0]['enabled'])
        self.assertEqual(result['pools'][0]['allowed_models'], ['current-model'])
        self.assertEqual(result['pool_pricing'][0]['multiplier_millionths'], 400000)
        self.assertEqual(result['pools'][0]['deployments'][0]['rpm'], 27)
        self.assertEqual(result['accounts']['new-account']['credential']['access_token'], 'PRIVATE_NEW_TOKEN')
        self.assertEqual(len(self.router.writes), 5)
        self.assertEqual(len(self.router.probes), 1)

    def test_account_conflict_keeps_editor_tokens_and_never_overwrites_same_account(self):
        from local_accounts import config_from
        self.page.locator('[data-action=edit-account]').click()
        credential = json.dumps({'provider':'openai','credential':{'access_token':'PRIVATE_DRAFT_TOKEN'}})
        self.page.locator('[name=credential]').fill(credential)
        self.page.locator('[name=proxy]').fill('socks5h://proxy.example:1080')
        self.pass_probe()
        update = config_from(self.app.remote)
        update['pools'][0]['deployments'][0]['enabled'] = False
        self.app.api('PUT', '/api/config', update)
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog [role=alert]')).to_contain_text('该账号已被修改')
        expect(self.page.locator('[name=credential]')).to_have_value(credential)
        expect(self.page.locator('#dialog')).to_be_visible()
        self.assertEqual(len(self.router.writes), 1)
        self.assertNotIn('PRIVATE_DRAFT_TOKEN', self.store.accounts.account(self.store.settings['router'], 'example-001')['credential_text'])
        self.page.locator('[data-action=close]').click()
        self.page.locator('[data-action=edit-account]').click()
        self.page.locator('[name=credential]').fill(credential)
        self.page.locator('[name=proxy]').fill('socks5h://proxy.example:1080')
        self.pass_probe()
        update = config_from(self.app.remote)
        update['accounts'] = {'credential-ref': {'provider':'openai','credential':{'access_token':'OTHER_TOKEN'}}}
        self.app.api('PUT', '/api/config', update)
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog [role=alert]')).to_contain_text('凭证或代理已被修改')
        expect(self.page.locator('[name=credential]')).to_have_value(credential)
        self.assertEqual(len(self.router.writes), 2)

    def test_connection_shows_key_id_without_passwords_or_control_credentials(self):
        account=next(iter(self.app.platform_client.sessions.values()))['account']
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#merchant-identity')).to_have_value(account['identity_id'])
        expect(self.page.locator('#merchant-profile option')).to_have_count(1)
        expect(self.page.locator('#merchant-auth, input[type=password], #merchant-password')).to_have_count(0)
        expect(self.page.get_by_role('button',name='注册新 ID',exact=True)).to_be_visible()
        expect(self.page.locator('#merchant-saved-id, #merchant-saved-secret, .platform-frame')).to_have_count(0)
        self.assertNotIn(self.store.settings['token'],self.page.content())
        self.page.reload()
        expect(self.page.locator('#merchant-identity')).to_have_value(account['identity_id'])

    def test_no_merchant_total_limits_and_pool_pricing_remains_editable(self):
        expect(self.page.locator('aside [data-id=limits]')).to_have_count(0)
        self.page.locator('[data-action=edit-pool][data-id=openai]').click()
        expect(self.page.locator('[name=multiplier]')).to_have_value('0.2')
        self.page.get_by_role('button', name='取消', exact=True).click()

    def test_logout_clears_pools_revenue_and_other_browser_cannot_read_them(self):
        from unittest.mock import patch
        self.page.locator('aside [data-id=revenue]').click()
        expect(self.page.get_by_text('累计应收 · USD', exact=True)).to_be_visible()
        self.page.locator('aside [data-id=connection]').click()
        with patch.object(self.app.platform_client, 'request', return_value=(200, {'ok':True}, {})):
            self.page.get_by_role('button', name='退出此 ID', exact=True).click()
            expect(self.page.get_by_role('button',name='注册新 ID',exact=True)).to_be_visible()
            self.page.wait_for_function('() => !connectionBusy')
        for section_name in ('accounts','revenue','receipts','customers','usage','refusals'):
            self.page.locator(f'aside [data-id={section_name}]').click()
            expect(self.page.locator('.panel.empty')).to_contain_text('请先登录商户账号')
            expect(self.page.locator('.platform-frame, #revenue-filter, #account-pools')).to_have_count(0)
        self.assertIsNone(self.page.evaluate('revenueData'))
        self.assertEqual(self.page.evaluate('pools()'), [])
        self.assertEqual(len(self.store.accounts.view(self.store.settings['router'])['pools']), 1)
        other = self.browser.new_context()
        try:
            page = other.new_page();page.goto(self.origin)
            expect(page.get_by_role('button',name='注册新 ID',exact=True)).to_be_visible()
            self.assertFalse(page.evaluate('state.auth.authenticated'))
        finally:
            other.close()

    def test_expiry_discards_late_revenue_result_and_modal(self):
        self.page.locator('[data-action=edit-pool][data-id=openai]').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.evaluate('''() => {
          window.originalRequest=window.teemerchant.request;
          window.teemerchant.request=(method,path,...rest)=>path.startsWith('/api/revenue')
            ?new Promise(resolve=>window.releaseRevenue=resolve):window.originalRequest(method,path,...rest);
          window.lateRevenue=loadRevenue().then(()=>false,()=>true);
        }''')
        for session in self.app.platform_client.sessions.values():session['expires']=0
        from unittest.mock import patch
        import httpx
        with patch.object(self.app.platform_client,'request',side_effect=httpx.ConnectError('offline')):
            self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('main')).not_to_contain_text('example-001')
        self.page.evaluate("releaseRevenue({ok:true,value:{data:[{pool:'PRIVATE_OLD_POOL'}]}})")
        self.assertFalse(self.page.evaluate('lateRevenue'))
        self.assertIsNone(self.page.evaluate('revenueData'))
        self.page.reload()
        expect(self.page.get_by_role('button',name='注册新 ID',exact=True)).to_be_visible()

    def test_local_identity_switch_keeps_pools_separate_and_logout_keeps_id(self):
        from test_platform_client import login_fixture
        from unittest.mock import patch
        browser=self.page.context.cookies()[0]['value']
        first=next(iter(self.app.platform_client.sessions.values()))['account']['identity_id']
        secret='c'*64
        merchant_id='m_'+hashlib.sha256(secret.encode()).hexdigest()[:32]
        self.app._api('POST','/api/credentials',{'merchant_id':merchant_id,'merchant_secret':secret})
        second=login_fixture(self.app,browser,'second')['account']['identity_id']
        self.page.reload()
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#merchant-identity')).to_have_value(second)
        expect(self.page.locator('#merchant-profile option')).to_have_count(2)
        self.assertEqual(self.page.evaluate('pools()'),[])
        self.page.locator('#merchant-profile').select_option(first)
        self.page.get_by_role('button',name='使用此 ID',exact=True).click()
        expect(self.page.locator('#merchant-identity')).to_have_value(first)
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.locator('main')).to_contain_text('example-001')
        self.page.locator('aside [data-id=connection]').click()
        with patch.object(self.app.platform_client,'request',return_value=(200,{'ok':True},{})):
            self.page.get_by_role('button',name='退出此 ID',exact=True).click()
            self.page.wait_for_function('() => !connectionBusy')
        expect(self.page.locator('#merchant-profile option')).to_have_count(2)
        self.page.locator('#merchant-profile').select_option(second)
        self.page.get_by_role('button',name='使用此 ID',exact=True).click()
        expect(self.page.locator('#merchant-identity')).to_have_value(second)
        self.assertEqual(self.page.evaluate('pools()'),[])

    def test_one_click_registration_keeps_private_seed_local(self):
        from unittest.mock import patch
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        original=self.app.platform_client.request
        challenges={}
        def request(client,method,url,headers,data=None):
            if url.endswith('/identity/challenge'):
                challenge={**data,'id':'ic_test','nonce':'nonce','expires':int(time.time())+300,'origin':'https://teerouter.org'}
                challenges['value']=challenge
                return 200,{'challenge':challenge},{}
            if url.endswith('/register'):
                challenge=challenges['value'];public=challenge['public_key']
                message=json.dumps(['teerouter-identity-v1',challenge['origin'],'register','merchant',public,challenge['id'],challenge['nonce'],challenge['expires']],separators=(',',':')).encode()
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(bytes.fromhex(data['signature']),message)
                self.assertNotIn('seed',data);self.assertNotIn('password',data)
                iid='m_sig_'+hashlib.sha256(bytes.fromhex(public)).hexdigest()
                account={'id':'p_new','identity_id':iid,'public_key':public,'role':'merchant','username':iid,'name':iid}
                return 201,{'account':account,'applications':[],'merchant_credentials_available':False,'csrf':'new-csrf'},{'set-cookie':'teerouter_portal='+'B'*43}
            return original(client,method,url,headers,data)
        with patch.object(self.app.platform_client,'request',side_effect=request):
            self.page.locator('aside [data-id=connection]').click()
            self.page.get_by_role('button',name='注册新 ID',exact=True).click()
            self.page.wait_for_function('() => state.auth?.account?.id === "p_new"')
            expect(self.page.locator('#merchant-identity')).to_have_value(re.compile('m_sig_[a-f0-9]{64}'))
            expect(self.page.locator('#merchant-profile option')).to_have_count(2)
            expect(self.page.locator('input[type=password]')).to_have_count(0)
            self.assertEqual(self.page.evaluate('pools()'),[])

    def test_revenue_totals_filter_pending_and_historic_bill_details(self):
        from test_revenue import receipt
        a=receipt('good-1');b=receipt('good-2');b['seq']=2;b['event']['pool']='other'
        b['event']['merchant_pricing']['pool']='other'
        c=receipt('pending');c['seq']=3;c['event']['status']=499
        c['event']['billing']={'version':1,'state':'pending','reason':'request_incomplete','items':'invalid'}
        from unittest.mock import patch
        self.store.settings['platform_url']='https://platform.example'
        from test_platform_client import login_fixture
        login_fixture(self.app, self.page.context.cookies()[0]['value'])
        mid=self.store.enrollment()['id']
        for row in (a,b,c):
            row['cursor']=row['seq']
            row['event']['merchant_id']=mid
            row['event']['settlement']={'state':'pending' if row is c else 'settled',
                'charge_microusd':None if row is c else '50','official_cost_microusd':None}
        from revenue import RevenueIndex, entry
        from urllib.parse import parse_qs
        index=RevenueIndex()
        for row in (a,b,c):
            item=entry('platform',row['seq'],row['event'])
            item.update(state='pending' if row is c else 'calculated',charge_microusd=None if row is c else '50',basis='platform_settlement')
            index.entries[('platform',row['seq'])]=item
        def read(path):
            if path=='withdrawals':
                from test_platform_data import withdrawals
                return withdrawals()
            if path.startswith('revenue'):
                return index.report(path.partition('?')[2])
            return next(row for row in (a,b,c) if str(row['seq'])==path.split('/')[-1])
        self.app.platform_data.fetch=read
        self.page.locator('aside [data-id=revenue]').click()
        expect(self.page.get_by_role('heading', name='收入', exact=True)).to_be_visible()
        cards=self.page.locator('.revenue-cards .card')
        expect(cards.nth(0)).to_contain_text('$0.000100')
        expect(cards.nth(3).locator('strong')).to_have_text('1')
        self.assertEqual(self.page.evaluate("money('9007199254740993')"),'$9,007,199,254.740993')
        self.page.locator('#revenue-filter [name=pool]').select_option('grok')
        self.page.get_by_role('button',name='筛选',exact=True).click()
        expect(self.page.locator('[data-action=revenue-detail]')).to_have_count(2)
        self.page.locator('[data-action=revenue-detail][data-id="0"]').click()
        expect(self.page.locator('#dialog')).to_contain_text('应收 待核算')
        self.page.get_by_role('button',name='关闭',exact=True).click()
        self.page.locator('[data-action=revenue-detail][data-id="1"]').click()
        expect(self.page.locator('#dialog')).to_contain_text('应收 $0.000050')
        expect(self.page.locator('#dialog')).to_contain_text('0.2×')
        expect(self.page.locator('#dialog')).to_contain_text('每百万 token')
        self.page.get_by_role('button',name='关闭',exact=True).click()
        self.page.get_by_role('button',name='重置',exact=True).click()
        expect(self.page.locator('[data-action=revenue-detail]')).to_have_count(3)
        self.assertEqual(self.router.writes, [])

    def test_edit_preserves_blank_secrets_and_reports_remote_save(self):
        self.page.locator('[data-action=edit-account]').click()
        expect(self.page.locator('[name=id]')).to_have_value('example-001')
        expect(self.page.locator('[name=credential]')).to_have_value('')
        expect(self.page.locator('[name=proxy]')).to_have_value('')
        self.page.locator('[name=max_concurrent]').fill('12')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('#toast')).to_contain_text('已保存到本地并同步至 Router')
        self.assertEqual(len(self.router.writes), 1)
        update = self.router.writes[0]
        self.assertEqual(update['accounts'], {})
        self.assertEqual(update['secrets'], {})
        self.assertEqual(update['pools'][0]['deployments'][0]['max_concurrent'], 12)
        self.assertEqual(update['pools'][0]['deployments'][0]['oauth_account'], 'credential-ref')
        self.assertEqual(update['pool_pricing'][0]['multiplier_millionths'], 200000)

    def test_pool_model_selection_syncs_once_for_all_accounts_and_keeps_unavailable_choices(self):
        pool = self.router.remote['pools'][0]
        pool['deployments'].append({**pool['deployments'][0], 'id': 'example-002'})
        self.router.remote['catalog']['data'] = [{'pool': 'openai', 'deployment': d['id'],
            'official_models': {f'model-{i:02}': {} for i in range(20)}} for d in pool['deployments']]
        # Both fixture accounts already exist upstream. Staging a new local
        # account without credentials correctly quarantines it before upload.
        self.app.accept(copy.deepcopy(self.router.remote))
        from local_accounts import config_from
        self.app.api('PUT', '/api/config', config_from(self.router.remote))
        self.router.writes.clear()
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        self.page.locator('[name=model_mode]').select_option('selected')
        self.assertEqual(self.page.locator('[name=allowed_model]').count(), 20)
        self.page.locator('#pool-model-none').click()
        for i in range(3):
            self.page.locator(f'[name=allowed_model][value="model-{i:02}"]').check()
        expect(self.page.locator('#pool-model-count')).to_have_text('已勾选 3 / 20')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#pool-model-editor')).to_have_count(0)
        expected = ['model-00', 'model-01', 'model-02']
        self.assertEqual(self.router.writes[-1]['pools'][0]['allowed_models'], expected)
        self.assertEqual(len(self.router.writes[-1]['pools'][0]['deployments']), 2)
        self.router.remote['catalog']['data'] = []
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        self.assertEqual(self.page.locator('[name=allowed_model]:checked').count(), 3)
        expect(self.page.locator('.model-options')).to_contain_text('暂未获取到目录')
        self.page.locator('#pool-model-none').click()
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#pool-model-editor')).to_have_count(0)
        self.assertEqual(self.router.writes[-1]['pools'][0]['allowed_models'], [])
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        self.page.locator('[name=model_mode]').select_option('all')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#pool-model-editor')).to_have_count(0)
        self.assertEqual(self.router.writes[-1]['pools'][0]['allowed_models'], [])

    def test_successful_refresh_removes_missing_model_from_list_and_editor(self):
        from local_accounts import config_from
        config = config_from(self.router.remote)
        config['pools'][0]['allowed_models'] = ['removed', 'kept']
        self.app.api('PUT', '/api/config', config)
        self.router.remote['catalog']['data'] = [{'pool': 'openai', 'deployment': 'example-001',
            'official_models': {'removed': {}, 'kept': {}}}]
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.locator('#pool-model-catalog')).to_contain_text('removed')
        self.router.remote['catalog']['data'] = [{'pool': 'openai', 'deployment': 'example-001',
            'official_models': {'kept': {}, 'new': {}}, 'catalog': {
                'scope': 'pool', 'complete_catalog': True, 'refreshing': False, 'last_error': None,
                'source_deployment': 'example-001', 'fetched_at': int(time.time()),
                'refresh_secs': 300, 'models': 2}}]
        self.page.locator('#pool-model-catalog [data-action=refresh]').click()
        expect(self.page.locator('#pool-model-catalog')).not_to_contain_text('removed')
        expect(self.page.locator('#pool-model-catalog')).to_contain_text('kept')
        self.assertEqual(self.router.remote['pools'][0]['allowed_models'], ['kept'])
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        expect(self.page.locator('[name=allowed_model][value=removed]')).to_have_count(0)
        expect(self.page.locator('[name=allowed_model][value=kept]')).to_be_checked()
        expect(self.page.locator('[name=allowed_model][value=new]')).not_to_be_checked()

    def test_failed_provider_refresh_clears_display_but_preserves_saved_model_selection(self):
        from local_accounts import config_from
        config = config_from(self.router.remote)
        config['pools'][0]['allowed_models'] = ['fixture-discovered-model']
        self.app.api('PUT', '/api/config', config)
        self.discover_fixture_model()
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#pool-model-catalog')).to_contain_text('fixture-discovered-model')
        self.router.remote['catalog']['data'][0].update({'official_models': {}, 'catalog': {
            'scope': 'provider', 'provider': 'openai', 'complete_catalog': False, 'models': 0,
            'fetched_at': None, 'refresh_secs': 300, 'refreshing': False, 'last_error': 'timeout'}})
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#pool-model-catalog')).not_to_contain_text('fixture-discovered-model')
        expect(self.page.get_by_text('目录获取失败', exact=True)).to_be_visible()
        self.page.locator('#pool-model-catalog [data-action=edit-pool-models]').click()
        expect(self.page.locator('[name=allowed_model][value=fixture-discovered-model]')).to_be_checked()

    def test_inline_model_draft_survives_navigation_and_rejects_stale_save(self):
        self.router.remote['catalog']['data'] = [{'pool': 'openai', 'deployment': 'example-001',
            'official_models': {'model-a': {}, 'model-b': {}, 'model-c': {}}}]
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        catalog = self.page.locator('#pool-model-catalog')
        catalog.get_by_role('button', name='编辑开放模型').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(catalog.locator('#pool-model-editor')).to_be_visible()
        self.page.locator('[name=allowed_model][value=model-c]').uncheck()
        expect(self.page.locator('[name=model_mode]')).to_have_value('selected')
        expect(self.page.locator('#pool-model-count')).to_have_text('已勾选 2 / 3')
        catalog.get_by_role('button', name='清空勾选').click()
        self.page.locator('[name=allowed_model][value=model-a]').check()
        self.page.locator('#pool-model-search').fill('model-b')
        catalog.get_by_role('button', name='勾选筛选结果').click()
        expect(self.page.locator('#pool-model-count')).to_have_text('已勾选 2 / 3')
        self.page.locator('aside [data-id=overview]').click()
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.locator('#pool-model-count')).to_have_text('已勾选 2 / 3')
        expect(self.page.locator('#pool-model-search')).to_have_value('model-b')
        self.router.remote['settings']['revision'] += 1
        self.app.accept(copy.deepcopy(self.router.remote))
        catalog.get_by_role('button', name='保存并同步').click()
        expect(catalog.get_by_role('alert')).to_contain_text('配置已变更')
        self.assertEqual(self.router.writes, [])
        expect(self.page.locator('[name=allowed_model][value=model-a]')).to_be_checked()
        catalog.get_by_role('button', name='取消编辑').click()
        expect(self.page.locator('#pool-model-editor')).to_have_count(0)
        self.assertEqual(self.router.writes, [])

    def test_expired_access_only_account_is_distinct_from_reauthorization(self):
        self.router.remote['accounts']['data'][0]['health'] = {'state': 'expired', 'last_error': 'access_token_expired'}
        self.app.accept(copy.deepcopy(self.router.remote))
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.get_by_text('已过期', exact=True)).to_be_visible()
        expect(self.page.get_by_text('需重新授权', exact=True)).to_have_count(0)
        self.assertEqual(self.router.writes, [])

    def test_pool_rate_failure_keeps_dialog_and_confirmed_price_then_retry_succeeds(self):
        self.page.locator('[data-action=edit-pool][data-id=openai]').click()
        self.page.locator('[name=multiplier]').fill('0.05')
        self.router.reject = True
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog .error')).to_contain_text('倍率未获 Router 确认')
        expect(self.page.locator('#dialog')).to_be_visible()
        expect(self.page.locator('[name=multiplier]')).to_have_value('0.05')
        self.assertEqual(self.page.evaluate("state.remote.settings.pool_pricing[0].multiplier_millionths"), 200000)
        self.assertIsNone(self.store.accounts.pending(self.store.settings['router']))
        self.assertEqual(self.router.writes, [])
        self.router.reject = False
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.get_by_text('倍率已更新', exact=True)).to_be_visible()
        self.assertEqual(self.page.evaluate("state.remote.settings.pool_pricing[0].multiplier_millionths"), 50000)
        self.assertIsNone(self.store.accounts.pending(self.store.settings['router']))

    def test_pool_lifecycle_empty_pool_edit_rate_then_add_account(self):
        self.page.locator('[data-action=pool]').click()
        self.page.locator('[name=name]').fill('claude-team')
        self.page.locator('[name=provider]').select_option('anthropic')
        self.page.locator('[name=multiplier]').fill('0.3')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.selected-row')).to_contain_text('claude-team')
        expect(self.page.locator('.selected-row td').nth(2)).to_have_text('0')
        expect(self.page.get_by_role('heading', name='claude-team · 账号（0）')).to_be_visible()
        created = self.router.writes[-1]
        self.assertEqual([p['name'] for p in created['pools']], ['openai'])
        self.assertEqual([p['pool'] for p in created['pool_pricing']], ['openai'])
        self.assertEqual(created['accounts'], {})
        self.assertEqual(created['secrets'], {})
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=account-pool][data-id=claude-team]').click()
        self.page.locator('[data-action=edit-pool][data-id=claude-team]').click()
        expect(self.page.locator('[name=name]')).to_have_attribute('readonly', '')
        expect(self.page.locator('[name=provider]')).to_be_disabled()
        expect(self.page.locator('[name=multiplier]')).to_have_value('0.3')
        self.page.locator('[name=multiplier]').fill('0.25')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        expect(self.page.locator('[name=pool]')).to_have_value('claude-team')
        expect(self.page.locator('[name=pool]')).to_have_attribute('readonly', '')
        expect(self.page.locator('select[name=pool]')).to_have_count(0)
        self.assertEqual(self.page.locator('#dialog [name=provider], #dialog [name=multiplier]').count(), 0)
        self.page.locator('[name=id]').fill('claude-account')
        self.page.locator('[name=credential]').fill(json.dumps({'provider':'anthropic','credential':{'api_key':'FIXTURE_CLAUDE_KEY'}}))
        self.page.locator('[name=proxy]').fill('https://proxy.example')
        self.pass_probe()
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.selected-row td').nth(2)).to_have_text('1')
        self.assertEqual(len(self.router.writes), 3)
        added = self.router.writes[-1]
        self.assertEqual(self.router.probes[-1]['draft_pool'], {'provider':'anthropic','allowed_models':None})
        self.assertEqual(added['pools'][1]['deployments'][0]['provider'], 'anthropic')
        self.assertEqual(added['pool_pricing'], [{'pool':'openai','multiplier_millionths':200000},
                                              {'pool':'claude-team','multiplier_millionths':250000}])
        self.assertEqual(added['pools'][0], created['pools'][0])
        self.page.locator('[data-action=edit-account][data-id=claude-account]').click()
        expect(self.page.locator('[name=pool] option')).to_have_count(1)
        self.assertEqual(json.loads(self.page.locator('[name=credential]').input_value())['credential']['api_key'], 'FIXTURE_CLAUDE_KEY')

    def test_empty_state_requires_pool_before_accounts(self):
        empty = self.app.platform_client.snapshot(self.page.context.cookies()[0]['value'])
        empty['remote']['pools'] = []
        empty['remote']['settings']['pool_pricing'] = []
        self.page.route('**/api/state', lambda route: route.fulfill(json=empty))
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        expect(self.page.locator('[data-action=pool]')).to_be_visible()
        expect(self.page.locator('[data-action=account], [data-action=import-accounts]')).to_have_count(0)
        self.page.locator('[data-action=pool]').click()
        expect(self.page.get_by_role('heading', name='添加账号池', exact=True)).to_be_visible()
        self.assertEqual(self.router.writes, [])

    def test_batch_unknown_pool_is_rejected_without_creating_it(self):
        self.page.locator('[data-action=import-accounts]').click()
        self.page.locator('[name=entries]').fill(json.dumps([
            {'id':'new-account','pool':'not-created','api_key':'FIXTURE_KEY','proxy':'https://proxy.example'}]))
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#dialog .error')).to_contain_text('请先添加账号池')
        expect(self.page.get_by_role('button', name='确认导入')).to_be_disabled()
        self.assertEqual(self.router.writes, [])

    def test_batch_file_preview_duplicate_block_and_single_commit(self):
        self.page.locator('[data-action=import-accounts]').click()
        self.page.locator('[name=pool]').select_option('openai')
        expect(self.page.locator('#dialog [name=proxy]')).to_have_count(0)
        rows = [{'id': 'new-001', 'credential': {'access_token': 'PRIVATE_OAUTH_TOKEN'},
                 'proxy': {'scheme':'socks5h','host':'proxy-1.example','port':1080,
                           'username':'PRIVATE_USER','password':'PRIVATE_PROXY_PASSWORD'}},
                {'id': 'new-002', 'api_key': 'PRIVATE_API_KEY',
                 'proxy': 'https://user:PRIVATE_PASSWORD@proxy-2.example:8443'}]
        self.page.locator('#import-files').set_input_files([
            {'name': 'first.json', 'mimeType': 'application/json', 'buffer': json.dumps(rows[0]).encode()},
            {'name': 'second.json', 'mimeType': 'application/json', 'buffer': json.dumps(rows[1]).encode()}])
        self.page.wait_for_function("document.querySelector('[name=entries]').value.includes('new-002')")
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#import-review')).to_contain_text('2 个账号校验通过')
        expect(self.page.locator('#import-review tbody tr').nth(0)).to_contain_text('socks5h://***:***@proxy-1.example:1080')
        expect(self.page.locator('#import-review tbody tr').nth(1)).to_contain_text('https://***:***@proxy-2.example:8443')
        self.assertNotIn('PRIVATE_', self.page.locator('#import-review').inner_text())
        self.assertEqual(self.router.writes, [])
        missing = copy.deepcopy(rows)
        del missing[1]['proxy']
        self.page.locator('[name=entries]').fill(json.dumps(missing))
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#dialog .error')).to_contain_text('第 2 个账号：缺少 proxy')
        expect(self.page.get_by_role('button', name='确认导入')).to_be_disabled()
        self.assertEqual(self.router.writes, [])
        self.page.locator('[name=entries]').fill(json.dumps([rows[0], rows[0]]))
        expect(self.page.get_by_role('button', name='确认导入')).to_be_disabled()
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#dialog .error')).to_contain_text('重复')
        self.assertEqual(self.router.writes, [])
        self.page.locator('[name=entries]').fill(json.dumps(rows))
        self.page.get_by_role('button', name='校验并预览').click()
        self.page.get_by_role('button', name='确认导入').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes), 1)
        self.assertEqual(len(self.router.writes[0]['pools'][0]['deployments']), 3)
        self.assertEqual(self.router.writes[0]['accounts']['new-001']['credential']['access_token'], 'PRIVATE_OAUTH_TOKEN')
        self.assertEqual(self.router.writes[0]['secrets']['new-002'], 'PRIVATE_API_KEY')
        self.assertEqual(self.router.writes[0]['accounts']['new-001']['proxy'],
                         'socks5h://PRIVATE_USER:PRIVATE_PROXY_PASSWORD@proxy-1.example:1080')
        self.assertEqual(self.router.writes[0]['pools'][0]['deployments'][2]['proxy'], rows[1]['proxy'])
        self.assertNotIn('PRIVATE_', self.page.locator('body').inner_text())
        self.assertEqual(self.page.locator('#dialog textarea').count(), 0)
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        for account_id, proxy in [('new-001',self.router.writes[0]['accounts']['new-001']['proxy']),
                                  ('new-002',rows[1]['proxy'])]:
            self.page.locator('[data-action=edit-account][data-id='+account_id+']').click()
            expect(self.page.locator('[name=proxy]')).to_have_value(proxy)
            self.page.locator('[data-action=close]').click()

    def test_batch_download_example_has_individual_structured_proxies(self):
        self.page.locator('[data-action=import-accounts]').click()
        with self.page.expect_download() as download:
            self.page.locator('#import-example').click()
        rows = json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['pool'], 'openai')
            self.assertEqual(set(row['proxy']), {'scheme','host','port','username','password'})
        self.assertNotEqual(rows[0]['proxy']['host'], rows[1]['proxy']['host'])
        self.page.locator('[name=entries]').fill(json.dumps(rows))
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#import-review')).to_contain_text('2 个账号校验通过')
        self.assertNotIn('REPLACE_PROXY_', self.page.locator('#import-review').inner_text())
        self.assertEqual(self.router.writes, [])

    def test_revision_conflict_retains_local_edit_without_claiming_router_success(self):
        self.router.reject = True
        self.page.locator('[data-action=edit-account]').click()
        self.page.locator('[name=max_concurrent]').fill('12')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.notice:not([id])')).to_contain_text('等待同步')
        expect(self.page.locator('.notice:not([id])')).to_contain_text('revision_conflict')
        expect(self.page.locator('#toast')).to_contain_text('已保存到本地，等待同步')
        self.assertEqual(self.router.writes, [])
        self.page.locator('[data-action=edit-account]').click()
        expect(self.page.locator('[name=max_concurrent]')).to_have_value('12')
        self.page.locator('[data-action=close]').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()

    def test_openai_device_code_fills_draft_then_requires_test_and_save(self):
        from unittest.mock import patch
        from test_device_oauth import DEVICE, APPROVED, TOKEN
        from oauth_network import OAuthError
        approved=threading.Event()
        def network(provider,url,proxy,**kwargs):
            self.assertEqual(proxy,'http://proxy.example:8080')
            if url.endswith('/usercode'):return DEVICE
            if url.endswith('/deviceauth/token'):
                if not approved.is_set():raise OAuthError('pending',status=403)
                return APPROVED
            return TOKEN
        with patch('oauth_service.request',side_effect=network):
            self.page.locator('[data-action=account]').click()
            self.page.locator('#account-oauth-tab').click()
            self.page.locator('[name=id]').fill('device-account')
            self.page.locator('[name=proxy]').fill('http://proxy.example:8080')
            self.page.get_by_role('button',name='设备码授权',exact=True).click()
            expect(self.page.locator('#oauth-device-code')).to_have_value('ABCD-EFGH')
            expect(self.page.get_by_role('button',name='复制设备码')).to_be_visible()
            expect(self.page.get_by_label('授权网址',exact=True)).to_have_value('https://auth.openai.com/codex/device')
            self.assertEqual(self.page.locator('#oauth-submit').count(),0)
            self.assertEqual(self.page.locator('[name=code]').count(),0)
            expect(self.page.get_by_role('button',name='保存并同步')).to_be_disabled()
            self.assertEqual(self.router.writes,[])
            approved.set()
            with self.app.state_lock:
                for flow in self.app.oauth.flows.values():flow['next_poll']=0
            expect(self.page.locator('#oauth-result')).to_be_visible(timeout=10000)
            value=json.loads(self.page.locator('[name=oauth_credential]').input_value())
            self.assertEqual(value['credential']['access_token'],'PRIVATE_ACCESS')
            self.assertEqual(value['credential']['refresh_token'],'PRIVATE_REFRESH')
            self.assertFalse(self.app.oauth.flows)
            self.assertEqual(self.router.writes,[])
            expect(self.page.get_by_role('button',name='保存并同步')).to_be_disabled()
            self.pass_probe()
            self.page.get_by_role('button',name='保存并同步').click()
            expect(self.page.locator('#dialog')).not_to_be_visible()
            self.assertEqual(len(self.router.writes),1)
            self.assertNotIn('PRIVATE_REFRESH',json.dumps(self.router.writes))
            self.assertEqual(self.errors,[])

    def test_add_oauth_then_rotate_credentials_without_replacing_proxy(self):
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        self.page.locator('[name=id]').fill('new-oauth')
        expect(self.page.locator('[name=pool]')).to_have_value('openai')
        credential = {'provider': 'openai', 'proxy': 'socks5h://proxy.example:1080',
                      'credential': {'access_token': 'FIXTURE_ACCESS', 'refresh_token': 'FIXTURE_REFRESH'}}
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.page.locator('[name=proxy]').fill(credential['proxy'])
        self.pass_probe()
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[0]['accounts']['new-oauth']['credential'], access_only(credential['credential']))
        self.page.locator('[data-action=edit-account][data-id=new-oauth]').click()
        expect(self.page.locator('[name=credential]')).to_have_value(json.dumps(
            {'provider':'openai','credential':credential['credential']},ensure_ascii=False,indent=2))
        expect(self.page.locator('[name=proxy]')).to_have_value('socks5h://proxy.example:1080')
        del credential['proxy']
        credential['credential']['access_token'] = 'ROTATED_FIXTURE_ACCESS'
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.pass_probe()
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes), 2)
        self.assertEqual(self.router.writes[1]['accounts']['new-oauth'], {**credential,'credential':access_only(credential['credential'])})

    def test_offline_restart_displays_local_credentials_and_preserves_pending_changes(self):
        self.app.connected = False
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        self.page.locator('[name=id]').fill('offline-key')
        expect(self.page.locator('[name=pool]')).to_have_value('openai')
        self.page.locator('[name=credential]').fill(json.dumps({'provider':'openai','credential':{'api_key':'LOCAL_PRIVATE_FIXTURE'}}))
        self.page.locator('[name=proxy]').fill('https://user:FIXTURE_PASSWORD@proxy.example')
        self.page.locator('#account-test').click()
        expect(self.page.locator('#dialog [role=alert]')).to_contain_text('请先连接 Router')
        expect(self.page.get_by_role('button', name='保存并同步')).to_be_disabled()
        self.app.connected = True
        self.pass_probe()
        self.app.connected = False
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.notice:not([id])')).to_contain_text('等待同步')
        self.assertEqual(self.router.writes, [])
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=edit-account][data-id=offline-key]').click()
        self.assertEqual(json.loads(self.page.locator('[name=credential]').input_value())['credential']['api_key'], 'LOCAL_PRIVATE_FIXTURE')
        expect(self.page.locator('[name=proxy]')).to_have_value('https://user:FIXTURE_PASSWORD@proxy.example')
        self.page.locator('[name=max_concurrent]').fill('20')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes, [])
        self.page.locator('[data-action=sync-config]').click()
        expect(self.page.locator('.notice:not([id])')).to_contain_text('已与 Router 同步')
        self.assertEqual(self.router.writes[0]['secrets']['offline-key'], 'LOCAL_PRIVATE_FIXTURE')

    def test_grok_native_paste_batch_and_local_edit(self):
        cache_key = 'https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828'
        native = {cache_key:{'key':'PRIVATE_GROK_ACCESS','refresh_token':'PRIVATE_GROK_REFRESH',
                             'expires_at':'2030-01-01T00:00:00.000Z','scope':'openid offline_access','token_type':'Bearer'}}
        expected = {'access_token':'PRIVATE_GROK_ACCESS','refresh_token':'PRIVATE_GROK_REFRESH',
                    'expires_at':1893456000,'scope':'openid offline_access','token_type':'Bearer'}
        self.page.locator('[data-action=pool]').click()
        self.page.locator('[name=name]').fill('grok')
        self.page.locator('[name=provider]').select_option('xai')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.page.locator('[data-action=account]').click()
        self.page.locator('#account-json-tab').click()
        self.page.locator('[name=id]').fill('grok-native')
        self.page.locator('[name=credential]').fill(json.dumps(native))
        self.page.locator('[name=proxy]').fill('socks5h://proxy-1.example:1080')
        self.pass_probe()
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['accounts']['grok-native']['credential'], access_only(expected))
        self.assertEqual(self.router.writes[-1]['accounts']['grok-native']['proxy'], 'socks5h://proxy-1.example:1080')
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=account-pool][data-id=grok]').click()
        self.page.locator('[data-action=edit-account][data-id=grok-native]').click()
        expect(self.page.locator('[name=credential]')).to_have_value(json.dumps(
            {'provider':'xai','credential':expected},ensure_ascii=False,indent=2))
        self.page.locator('[name=credential]').fill(json.dumps(native))
        self.page.locator('[name=max_concurrent]').fill('12')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['accounts'], {})
        self.assertEqual(self.router.writes[-1]['secrets'], {})
        self.page.locator('[data-action=import-accounts]').click()
        with self.page.expect_download() as download:
            self.page.locator('#import-example').click()
        example = json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
        self.assertIn(cache_key, example[0]['credential'])
        self.assertIn('api_key', example[1])
        self.page.locator('[name=entries]').fill(json.dumps([
            {'id':'grok-batch','credential':native,'proxy':{'scheme':'http','host':'proxy-2.example','port':8080}}]))
        self.page.locator('#import-preview').click()
        expect(self.page.locator('#import-review')).to_contain_text('1 个账号校验通过')
        self.assertNotIn('PRIVATE_GROK', self.page.locator('#import-review').inner_text())
        self.page.get_by_role('button', name='确认导入').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['accounts']['grok-batch']['credential'], access_only(expected))
        self.assertEqual(self.router.writes[-1]['accounts']['grok-batch']['proxy'], 'http://proxy-2.example:8080/')
        self.assertNotIn('PRIVATE_GROK', self.page.locator('body').inner_text())




    def test_platform_records_are_queried_on_open_without_sync_controls_or_local_writes(self):
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('[data-action=sync]')).to_have_count(0)
        self.assertEqual([p for p in self.platform_reads if p!='state'], [])
        rows=[];writes=[]
        def query(path):
            self.platform_reads.append(path)
            if path=='state':return copy.deepcopy(self.router.remote)
            return {'data':copy.deepcopy(rows),'has_more':False,'next_before':None}
        self.app.platform_data.fetch=query
        self.store.db.set_trace_callback(lambda sql:writes.append(sql) if sql.split()[0] in ('INSERT','UPDATE','DELETE') else None)
        self.page.locator('aside [data-id=refusals]').click()
        expect(self.page.get_by_role('heading',name='拒答记录',exact=True)).to_be_visible()
        expect(self.page.locator('tbody')).to_contain_text('暂无记录')
        self.page.wait_for_function('() => recordPage.loadedAt>0')
        rows.append({'seq':'17','event':{'model':'fixture-model','refusal_type':'provider_refusal'}})
        # Re-entering inside the one-minute polling window still reads now.
        self.page.locator('aside [data-id=connection]').click()
        self.page.locator('aside [data-id=refusals]').click()
        expect(self.page.locator('tbody')).to_contain_text('fixture-model')
        self.assertEqual([p for p in self.platform_reads if p!='state'],['activity?kind=refusals&limit=50']*2)
        self.assertEqual(writes,[])
        self.page.locator('[data-action=record]').click()
        expect(self.page.locator('#dialog')).to_contain_text('provider_refusal')

    def test_slow_previous_page_does_not_block_revenue_or_overwrite_reentry(self):
        from revenue import RevenueIndex
        from test_platform_data import withdrawals
        release=threading.Event();started=threading.Event();reads=[]
        def query(path):
            if path=='withdrawals':return withdrawals()
            reads.append(path)
            if path.startswith('activity?'):
                started.set();release.wait(15)
                raise RuntimeError('old page unavailable')
            report=RevenueIndex().report('')
            report['summary']['charge_microusd']=str(1000000*sum(p.startswith('revenue') for p in reads))
            return report
        self.app.platform_data.fetch=query
        try:
            self.page.locator('aside [data-id=usage]').click()
            self.assertTrue(started.wait(5))
            self.page.locator('aside [data-id=revenue]').click()
            expect(self.page.locator('.revenue-cards').first).to_contain_text('$1.000000')
            self.page.locator('aside [data-id=connection]').click()
            self.page.locator('aside [data-id=revenue]').click()
            expect(self.page.locator('.revenue-cards').first).to_contain_text('$2.000000')
        finally:release.set()
        expect(self.page.locator('.content')).not_to_contain_text('old page unavailable')

    def test_revenue_reentry_discards_late_old_response(self):
        from revenue import RevenueIndex
        from test_platform_data import withdrawals
        release=threading.Event();started=threading.Event();reads=[]
        def query(path):
            if path=='withdrawals':return withdrawals()
            reads.append(path);number=len(reads)
            report=RevenueIndex().report('');report['summary']['charge_microusd']=str(number*1000000)
            if number==1:started.set();release.wait(15)
            return report
        self.app.platform_data.fetch=query
        try:
            self.page.locator('aside [data-id=revenue]').click()
            self.assertTrue(started.wait(5))
            self.page.locator('aside [data-id=connection]').click()
            self.page.locator('aside [data-id=revenue]').click()
            expect(self.page.locator('.revenue-cards').first).to_contain_text('$2.000000')
            with self.page.expect_response(lambda r:'/api/revenue?' in r.url):release.set()
            # Allow the old response handler to run before inspecting its result.
            self.page.wait_for_timeout(100)
            expect(self.page.locator('.revenue-cards').first).to_contain_text('$2.000000')
        finally:release.set()

    def test_customer_navigation_reads_current_router_state(self):
        self.router.remote['settings']['customers']=[{'user_id':'newly-used-customer','enabled':True,'models':[],
            'max_concurrent':2,'rpm':0,'tpm':0,'refusal_count':0}]
        self.page.locator('aside [data-id=customers]').click()
        expect(self.page.locator('.content')).to_contain_text('newly-used-customer')

    def test_embedded_page_navigation_queries_platform_on_every_entry(self):
        from test_platform_data import withdrawals
        from revenue import RevenueIndex
        reads=[];status='pending'
        def query(path):
            if path.startswith('revenue'):return RevenueIndex().report('')
            reads.append(path)
            if path=='withdrawals':return withdrawals(status)
            self.assertEqual(path,'receipts/mine')
            return {'data':[],'has_more':False}
        self.app.platform_data.fetch=query
        self.page.locator('aside [data-id=revenue]').click()
        frame=self.page.frame_locator('.platform-frame')
        expect(frame.locator('#withdrawal-form')).to_be_visible()
        self.page.frames[-1].evaluate("location.hash='receipts'")
        expect(frame.locator('.finance-records')).to_be_visible()
        status='paid'
        self.page.frames[-1].evaluate("location.hash='finance'")
        expect(frame.locator('tbody').last).to_contain_text('已打款')
        self.page.frames[-1].evaluate("location.hash='receipts'")
        self.page.wait_for_function("() => document.querySelector('.platform-frame').contentWindow.document.querySelector('.finance-records')!==null")
        self.assertEqual(reads,['withdrawals','receipts/mine','withdrawals','receipts/mine'])

    def test_remote_withdrawal_refresh_preserves_form_and_displays_platform_errors(self):
        from test_platform_data import withdrawals
        from revenue import RevenueIndex
        status='pending';offline=False
        def query(path):
            if path.startswith('revenue'):return RevenueIndex().report('')
            self.assertEqual(path,'withdrawals')
            if offline:raise RuntimeError('fixture platform offline')
            return withdrawals(status)
        self.app.platform_data.fetch=query
        self.page.locator('aside [data-id=revenue]').click()
        frame=self.page.frame_locator('.platform-frame')
        expect(frame.locator('#withdrawal-form')).to_be_visible()
        expect(self.page.locator('aside [data-id=finance]')).to_have_count(0)
        expect(self.page.locator('aside [data-id=revenue]')).to_have_text('收入')
        expect(self.page.locator('.revenue-cards')).to_be_visible()
        expect(frame.locator('header')).not_to_be_visible()
        frame.locator('[name=address]').fill('unsaved-address')
        frame.locator('[name=amount]').fill('0.5')
        self.page.frames[-1].evaluate('window.retainedWithdrawalForm=document.querySelector("#withdrawal-form")')
        # Income polling and filtering must not remount the withdrawal form.
        self.page.evaluate('refreshPlatformRecords(true)')
        self.page.locator('#revenue-filter [type=submit]').click()
        expect(frame.locator('[name=address]')).to_have_value('unsaved-address')
        self.assertTrue(self.page.frames[-1].evaluate('retainedWithdrawalForm===document.querySelector("#withdrawal-form")'))
        self.page.wait_for_function('() => document.querySelector(".income-withdrawals").getBoundingClientRect().height>500')
        status='paid'
        self.page.frames[-1].evaluate('refreshPlatformPage(true)')
        expect(frame.locator('tbody').last).to_contain_text('已打款')
        expect(frame.locator('[name=address]')).to_have_value('unsaved-address')
        expect(frame.locator('[name=amount]')).to_have_value('0.5')
        offline=True
        self.page.frames[-1].evaluate('refreshPlatformPage(true)')
        expect(frame.locator('.notice.warn')).to_contain_text('fixture platform offline')
        expect(frame.locator('tbody').last).to_contain_text('已打款')
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='platform_history'").fetchone()[0],0)

    def test_merged_income_can_submit_withdrawal_without_router_writes(self):
        from test_platform_data import withdrawals
        from revenue import RevenueIndex
        from unittest.mock import patch
        submitted=[]
        def query(path):
            if path.startswith('revenue'):return RevenueIndex().report('')
            result=withdrawals()
            result['balance']['available_microusd']='500000' if submitted else '1000000'
            return result
        self.app.platform_data.fetch=query
        def post(client,method,url,headers,data=None):
            self.assertEqual(method,'POST');self.assertTrue(url.endswith('/merchant-api/v1/withdrawals'))
            self.assertTrue(headers['Idempotency-Key'])
            submitted.append(data)
            return 201,{'id':'2'},{}
        self.page.locator('aside [data-id=revenue]').click()
        frame=self.page.frame_locator('.income-withdrawals')
        expect(frame.locator('#withdrawal-form')).to_be_visible()
        frame.locator('[name=address]').fill('fixture-address')
        frame.locator('[name=amount]').fill('0.5')
        with patch.object(self.app.platform_client,'request',side_effect=post):
            frame.locator('#withdrawal-form [type=submit]').click()
            expect(frame.locator('#toast')).to_contain_text('提现申请已提交')
        self.assertEqual(submitted,[{'network':'tron','amount':'0.5','address':'fixture-address','policy_version':1}])
        expect(frame.locator('.withdrawal-cards .card').first).to_contain_text('$0.50')
        expect(frame.locator('[name=address]')).to_have_value('')
        self.assertEqual(self.router.writes,[])

    def test_receipt_query_errors_are_visible_and_clear_after_automatic_retry(self):
        offline=False
        def query(path):
            self.assertEqual(path,'receipts/mine')
            if offline:raise RuntimeError('fixture receipt unavailable')
            return {'data':[],'has_more':False}
        self.app.platform_data.fetch=query
        self.page.locator('aside [data-id=receipts]').click()
        frame=self.page.frame_locator('.platform-frame')
        expect(frame.locator('.finance-records')).to_be_visible()
        offline=True
        self.page.frames[-1].evaluate('refreshPlatformPage(true)')
        expect(frame.locator('.notice.warn')).to_contain_text('fixture receipt unavailable')
        offline=False
        self.page.frames[-1].evaluate('refreshPlatformPage(true)')
        expect(frame.locator('.notice.warn')).to_have_count(0)


if __name__ == '__main__':
    unittest.main()
