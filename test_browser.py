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
        return copy.deepcopy(self.remote)


@unittest.skipUnless(sync_playwright, 'Optional Playwright dependency not installed')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.router = FakeRouter(self.store)
        self.app = Application(self.store, self.router)
        self.app.accept(copy.deepcopy(self.router.remote))
        self.archive_fetches = []
        def archive_page(url, secret, cursor):
            self.archive_fetches.append((url, cursor))
            return {'version': 1, 'merchant_id': self.store.enrollment()['id'], 'archive_id': 'a'*32,
                    'next_cursor': cursor, 'has_more': False, 'data': []}
        self.app.archive.fetch = archive_page
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
        update['pool_pricing'][0]['multiplier_millionths'] = 400000
        self.app.api('PUT', '/api/config', update)
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
        self.assertEqual(len(self.router.writes), 3)
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

    def test_connection_shows_username_and_login_without_manual_credentials(self):
        from unittest.mock import patch
        secret = self.store.settings['token']
        identity = self.store.enrollment()['id']
        self.store.remember_platform_identity(self.store.settings['platform_url'], identity,
            {'id': 'p_seller', 'role': 'merchant', 'username': 'seller', 'name': 'Seller'})
        requested = []
        self.page.on('request', lambda request: requested.append(request.url) if request.url.endswith('/api/credentials') else None)
        with patch.object(self.app.platform_client, 'refresh_identity', return_value=self.store.platform_identity()):
            self.page.locator('aside [data-id=connection]').click()
            expect(self.page.locator('#merchant-username')).to_have_value('seller')
        expect(self.page.locator('#merchant-saved-id, #merchant-saved-secret, [data-action=merchant-credentials]')).to_have_count(0)
        self.assertEqual(requested, [])
        denied = self.page.request.get(self.origin + '/api/credentials')
        self.assertEqual(denied.status, 403)
        self.assertNotIn(secret, denied.text())
        self.page.get_by_role('button', name='添加登录', exact=True).click()
        expect(self.page.get_by_role('heading', name='登录商户')).to_be_visible()
        expect(self.page.locator('.platform-frame')).to_have_count(0)
        expect(self.page.locator('aside [data-id=platform]')).to_have_count(0)
        expect(self.page.get_by_role('heading', name='Router 连接')).to_be_visible()
        self.assertNotIn(secret, json.dumps(self.app.api('GET','/api/state',{})))

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
            self.page.get_by_role('button', name='退出登录', exact=True).click()
            expect(self.page.locator('#merchant-auth')).to_be_visible()
            self.page.wait_for_function('() => !connectionBusy')
        for section_name in ('accounts','revenue','finance','receipts','customers','usage','refusals'):
            self.page.locator(f'aside [data-id={section_name}]').click()
            expect(self.page.locator('.panel.empty')).to_contain_text('请先登录商户账号')
            expect(self.page.locator('.platform-frame, #revenue-filter, #account-pools')).to_have_count(0)
        self.assertIsNone(self.page.evaluate('revenueData'))
        self.assertEqual(self.page.evaluate('pools()'), [])
        self.assertEqual(len(self.store.accounts.view(self.store.settings['router'])['pools']), 1)
        other = self.browser.new_context()
        try:
            page = other.new_page();page.goto(self.origin)
            expect(page.locator('#merchant-auth')).to_be_visible()
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
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('main')).not_to_contain_text('example-001')
        self.page.evaluate("releaseRevenue({ok:true,value:{data:[{pool:'PRIVATE_OLD_POOL'}]}})")
        self.assertTrue(self.page.evaluate('lateRevenue'))
        self.assertIsNone(self.page.evaluate('revenueData'))
        self.page.reload()
        expect(self.page.locator('#merchant-auth')).to_be_visible()

    def test_only_logged_in_accounts_appear_in_switcher_and_keep_separate_pools(self):
        import httpx
        from unittest.mock import patch
        secret = 'c'*64
        merchant_id = 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32]
        view = {'account':{'id':'p_second','username':'second','name':'Second','role':'merchant'},
                'applications':[{'kind':'merchant','status':'approved','subject':merchant_id}],
                'merchant_credentials_available':True}
        client = httpx.Client
        def fetch(request):
            if request.url.path.endswith('/reveal'):
                return httpx.Response(200,json={'merchant_id':merchant_id,'merchant_secret':secret})
            return httpx.Response(200,json={**view,'csrf':'csrf'},headers={'set-cookie':'teerouter_portal='+'B'*43})
        with patch('platform_client.httpx.Client',side_effect=lambda **kw:client(**kw,transport=httpx.MockTransport(fetch))):
            self.page.locator('aside [data-id=connection]').click()
            self.page.get_by_role('button',name='添加登录',exact=True).click()
            self.page.locator('#merchant-auth [name=username]').fill('second')
            self.page.locator('#merchant-auth [name=password]').fill('test-password')
            self.page.locator('#merchant-auth [type=submit]').click()
            expect(self.page.locator('#merchant-username')).to_have_value('second')
            expect(self.page.locator('#merchant-profile option')).to_have_count(2)
            self.assertEqual(self.page.evaluate('pools()'),[])
            self.page.locator('#merchant-profile').select_option('p_seller')
            self.page.get_by_role('button',name='切换',exact=True).click()
            expect(self.page.locator('#merchant-username')).to_have_value('seller')
            self.page.locator('aside [data-id=accounts]').click()
            expect(self.page.locator('main')).to_contain_text('example-001')
            self.page.locator('aside [data-id=connection]').click()
            self.page.get_by_role('button',name='退出登录',exact=True).click()
            self.page.wait_for_function('() => !connectionBusy')
            expect(self.page.locator('#merchant-profile option[value=p_seller]')).to_have_count(0)
            self.page.locator('#merchant-profile').select_option('p_second')
            self.page.get_by_role('button',name='切换',exact=True).click()
            expect(self.page.locator('#merchant-username')).to_have_value('second')
            self.assertEqual(self.page.evaluate('pools()'),[])

    def test_login_automatically_binds_and_connection_displays_login_username(self):
        self.app.platform_client.sessions.clear();self.app.platform_client.accounts.clear();self.page.reload()
        import httpx
        from unittest.mock import patch
        secret = 'b' * 64
        merchant_id = 'm_' + hashlib.sha256(secret.encode()).hexdigest()[:32]
        account = {'id': 'p_seller', 'role': 'merchant', 'username': 'seller', 'name': 'Seller'}
        view = {'account': account, 'merchant': {'id': merchant_id}, 'keys': [],
                'applications': [{'kind': 'merchant', 'status': 'approved', 'subject': merchant_id}],
                'merchant_credentials_available': True}
        original_client = httpx.Client
        def fetch(request):
            if request.url.path.endswith('/login'):
                return httpx.Response(200, json={**view, 'csrf': 'csrf'},
                    headers={'set-cookie': 'teerouter_portal=' + 'A' * 43})
            if request.url.path.endswith('/reveal'):
                return httpx.Response(200, json={'merchant_id': merchant_id, 'merchant_secret': secret})
            if request.url.path.endswith('/profile'):
                return httpx.Response(200, json={'account': account, 'merchant': {'id': merchant_id}, 'legacy': False})
            if request.url.path.endswith('/password'):
                self.assertEqual(json.loads(request.content), {'current_password': 'a-test-password', 'new_password': 'a-new-test-password'})
                return httpx.Response(200, json={'ok': True})
            if not request.headers.get('cookie'):
                return httpx.Response(401, json={'error': 'login_required'})
            return httpx.Response(200, json=view)
        with patch('platform_client.httpx.Client', side_effect=lambda **kw: original_client(**kw, transport=httpx.MockTransport(fetch))):
            self.page.locator('aside [data-id=connection]').click()
            self.page.locator('#merchant-auth [name=username]').fill('seller')
            self.page.locator('#merchant-auth [name=password]').fill('a-test-password')
            self.page.locator('#merchant-auth [type=submit]').click()
            expect(self.page.locator('#merchant-username')).to_have_value('seller')
            expect(self.page.locator('#platform-sync-status')).to_contain_text('账单已同步')
            self.assertIsNotNone(self.app.archive.status()['synced_at'])
            expect(self.page.get_by_role('button', name='绑定到本机')).to_have_count(0)
            self.assertEqual(self.store.enrollment()['id'], merchant_id)
            expect(self.page.get_by_role('heading', name='Router 连接')).to_be_visible()
            expect(self.page.locator('iframe')).to_have_count(0)
            self.assertNotIn(secret, self.page.content())
            self.page.get_by_role('button', name='修改密码', exact=True).click()
            self.page.locator('#merchant-password [name=current_password]').fill('a-test-password')
            self.page.locator('#merchant-password [name=new_password]').fill('a-new-test-password')
            self.page.locator('#merchant-password [type=submit]').click()
            expect(self.page.locator('#merchant-auth [name=password]')).to_have_value('')
            self.assertFalse(self.app.platform_client.sessions)
            self.assertEqual(self.store.enrollment()['id'], merchant_id)

    def test_platform_sync_status_updates_without_erasing_connection_drafts(self):
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#platform-sync-status')).to_contain_text('账单已同步')
        self.page.locator('#policy-path-tdx').fill('unsaved-policy.json')
        calls = len(self.archive_fetches)
        self.app.archive.error = 'fixture unavailable'
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#platform-sync-status')).to_have_text('账单同步失败')
        expect(self.page.locator('#platform-sync-error')).to_contain_text('fixture unavailable')
        self.app.archive.error = ''
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE platform_cursors SET synced_at=synced_at+1')
        self.page.evaluate('pollAccountAccess()')
        expect(self.page.locator('#platform-sync-status')).to_contain_text('账单已同步')
        expect(self.page.locator('#platform-sync-error')).to_be_hidden()
        expect(self.page.locator('#policy-path-tdx')).to_have_value('unsaved-policy.json')
        self.assertEqual(len(self.archive_fetches), calls)

    def test_platform_sync_is_immediate_nonblocking_and_late_result_is_discarded(self):
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#platform-sync-status')).to_contain_text('账单已同步')
        self.page.evaluate('''() => {
          window.originalRequest=window.teemerchant.request;
          window.syncCalls=0;
          window.teemerchant.request=(method,path,...rest)=>{
            if(path==='/api/sync'){syncCalls++;return new Promise(resolve=>window.releaseSync=resolve)}
            return window.originalRequest(method,path,...rest);
          };
          platformSyncScope='';void syncPlatformAutomatically();void syncPlatformAutomatically();
        }''')
        expect(self.page.locator('#platform-sync-status')).to_have_text('账单同步中')
        self.page.locator('#policy-path-tdx').fill('still-editable.json')
        self.assertEqual(self.page.evaluate('syncCalls'), 1)
        for session in self.app.platform_client.sessions.values():session['expires']=0
        self.page.evaluate('pollAccountAccess()')
        self.page.evaluate('releaseSync({ok:false,error:"PRIVATE_OLD_ACCOUNT_ERROR"})')
        self.page.wait_for_function('() => !platformSyncing')
        expect(self.page.locator('#platform-sync-status')).to_have_text('')
        expect(self.page.locator('#platform-sync-error')).to_be_hidden()
        self.assertNotIn('PRIVATE_OLD_ACCOUNT_ERROR', self.page.locator('body').inner_text())

    def test_inline_registration_pending_status_and_refresh_preserve_connection_drafts(self):
        self.app.platform_client.sessions.clear();self.app.platform_client.accounts.clear();self.page.reload()
        import httpx
        from unittest.mock import patch
        account = {'id': 'p_pending', 'role': 'merchant', 'username': 'pending', 'name': 'New merchant'}
        view = {'account': account, 'applications': [{'kind': 'merchant', 'status': 'pending'}],
                'merchant_credentials_available': False}
        original_client = httpx.Client
        calls = []
        def fetch(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/info'):
                return httpx.Response(200, json={'policy': {'registration_enabled': True}})
            if request.url.path.endswith('/register'):
                self.assertEqual(json.loads(request.content)['role'], 'merchant')
                return httpx.Response(201, json={**view, 'csrf': 'csrf'},
                    headers={'set-cookie': 'teerouter_portal=' + 'A' * 43})
            if request.url.path.endswith('/logout'):
                return httpx.Response(200, json={'ok': True})
            if request.url.path.endswith('/profile'):
                return httpx.Response(200, json={'account': {**account, 'username': 'Legacy name'},
                    'merchant': {'id': self.store.enrollment()['id']}, 'legacy': True})
            return httpx.Response(200, json=view)
        with patch('platform_client.httpx.Client', side_effect=lambda **kw: original_client(**kw, transport=httpx.MockTransport(fetch))):
            self.page.locator('aside [data-id=connection]').click()
            expect(self.page.get_by_role('heading', name='账号与连接', exact=True)).to_be_visible()
            self.page.get_by_role('tab', name='注册', exact=True).click()
            form = self.page.locator('#merchant-auth')
            form.locator('[name=username]').fill('pending')
            form.locator('[name=name]').fill('New merchant')
            form.locator('[name=password]').fill('registration-password')
            form.locator('[type=submit]').click()
            expect(self.page.locator('#merchant-username')).to_have_value('pending')
            expect(self.page.locator('#merchant-approval')).to_have_text('开通中')
            expect(self.page.locator('#connection')).to_have_count(0)
            self.page.locator('#platform-connection [name=platform_url]').fill('https://unsaved-platform.example')
            self.page.get_by_role('button', name='刷新状态', exact=True).click()
            expect(self.page.locator('#platform-connection [name=platform_url]')).to_have_value('https://unsaved-platform.example')
            self.page.get_by_role('button', name='退出登录', exact=True).click()
            expect(self.page.locator('#merchant-auth [name=password]')).to_have_value('')
            self.assertNotIn('/portal/api/merchant/credentials/reveal', calls)

    def test_slow_account_status_does_not_block_registration_or_erase_its_draft(self):
        self.app.platform_client.sessions.clear();self.app.platform_client.accounts.clear();self.page.reload()
        import httpx
        from unittest.mock import patch
        from platform_client import Reply
        entered, release = threading.Event(), threading.Event()
        original_call, original_client = self.app.platform_client.call, httpx.Client
        def delayed(session, mode, method, path, *args):
            if mode == 'account' and method == 'GET' and path == 'me':
                entered.set();release.wait(10)
                return Reply(401, b'{"error":"login_required"}')
            return original_call(session, mode, method, path, *args)
        def fetch(request):
            self.assertTrue(request.url.path.endswith('/info'))
            return httpx.Response(200, json={'policy': {'registration_enabled': True}})
        with patch.object(self.app.platform_client, 'call', side_effect=delayed), \
             patch('platform_client.httpx.Client', side_effect=lambda **kw: original_client(**kw, transport=httpx.MockTransport(fetch))):
            try:
                self.page.locator('aside [data-id=connection]').click()
                self.assertTrue(entered.wait(2))
                self.page.get_by_role('tab', name='注册', exact=True).click()
                expect(self.page.locator('#merchant-auth [name=name]')).to_be_visible()
                self.page.locator('#merchant-auth [name=username]').fill('draft_merchant')
                release.set()
                self.page.wait_for_function('() => !connectionLoading')
                expect(self.page.locator('#merchant-auth [name=username]')).to_have_value('draft_merchant')
                expect(self.page.locator('#merchant-auth [name=name]')).to_be_visible()
            finally:
                release.set()

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
        batch={'version':1,'archive_id':'a'*32,'merchant_id':mid,'data':[a,b,c],'next_cursor':3,'has_more':False}
        with patch.object(self.app.archive,'fetch',return_value=batch): self.app.sync()
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
        self.assertNotIn('allowed_models', self.router.writes[-1]['pools'][0])

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
        expect(self.page.locator('.notice')).to_contain_text('等待同步')
        expect(self.page.locator('.notice')).to_contain_text('revision_conflict')
        expect(self.page.locator('#toast')).to_contain_text('已保存到本地，等待同步')
        self.assertEqual(self.router.writes, [])
        self.page.locator('[data-action=edit-account]').click()
        expect(self.page.locator('[name=max_concurrent]')).to_have_value('12')
        self.page.locator('[data-action=close]').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()

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
        expect(self.page.locator('.notice')).to_contain_text('等待同步')
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
        expect(self.page.locator('.notice')).to_contain_text('已与 Router 同步')
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


if __name__ == '__main__':
    unittest.main()
