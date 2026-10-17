"""Optional UI integration tests: pip install playwright; Microsoft Edge required.

Uses an isolated local server and a fake TEE transport. Never touches live accounts.
"""
import copy
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import time
import threading
import unittest
from app import Application, handler
from store import Store
try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


class FakeRouter:
    def __init__(self, store):
        self.writes = []
        self.reject = False
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
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(self.app, self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel='msedge', headless=True)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1100})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
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

    def test_merchant_credentials_show_locally_refill_edit_and_clear_on_navigation(self):
        secret = self.store.settings['token']
        identity = self.store.enrollment()['id']
        requested = []
        self.page.on('request', lambda request: requested.append(request.url) if request.url.endswith('/api/credentials') else None)
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#merchant-saved-id')).to_have_value(identity)
        expect(self.page.locator('#merchant-credential-status')).to_contain_text('当前连接使用此凭证')
        expect(self.page.locator('#merchant-saved-secret')).to_have_value('********')
        self.assertEqual(requested, [])
        denied = self.page.request.get(self.origin + '/api/credentials')
        self.assertEqual(denied.status, 403)
        self.assertNotIn(secret, denied.text())
        self.page.locator('[data-action=merchant-secret]').click()
        expect(self.page.locator('#merchant-saved-secret')).to_have_value(secret)
        expect(self.page.locator('#merchant-saved-secret')).to_have_attribute('type','text')
        self.page.locator('[data-action=merchant-secret]').click()
        expect(self.page.locator('#merchant-saved-secret')).to_have_value('********')
        self.page.locator('[data-action=merchant-secret]').click()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('aside [data-id=connection]').click()
        expect(self.page.locator('#merchant-saved-secret')).to_have_value('********')
        self.page.locator('[data-action=merchant-credentials]').click()
        expect(self.page.locator('#dialog [name=merchant_id]')).to_have_value(identity)
        expect(self.page.locator('#dialog [name=merchant_secret]')).to_have_value(secret)
        expect(self.page.locator('#dialog [name=merchant_secret]')).to_have_attribute('type','password')
        self.page.locator('[data-action=merchant-edit-secret]').click()
        expect(self.page.locator('#dialog [name=merchant_secret]')).to_have_attribute('type','text')
        self.page.get_by_role('button', name='取消', exact=True).click()
        expect(self.page.locator('#dialog [name=merchant_secret]')).to_have_count(0)
        self.assertNotIn(secret, json.dumps(self.app.api('GET','/api/state',{})))

    def test_limits_preserve_pool_pricing(self):
        self.page.get_by_role('button', name='限速', exact=True).click()
        expect(self.page.get_by_role('heading', name='限速', exact=True)).to_be_visible()
        expect(self.page.locator('main')).not_to_contain_text('倍率')
        expect(self.page.locator('main')).not_to_contain_text('费率')
        self.page.get_by_role('button', name='调整限速').click()
        self.page.locator('[name=max_concurrent]').fill('8')
        self.page.locator('[name=rpm]').fill('120')
        self.page.locator('[name=tpm]').fill('100000')
        self.page.get_by_role('button', name='保存', exact=True).click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes),1)
        update=self.router.writes[0]
        self.assertEqual((update['max_concurrent'],update['rpm'],update['tpm']),(8,120,100000))
        self.assertEqual(update['pool_pricing'],[{'pool':'openai','multiplier_millionths':200000}])
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=edit-pool][data-id=openai]').click()
        expect(self.page.locator('[name=multiplier]')).to_have_value('0.2')
        self.page.get_by_role('button', name='取消', exact=True).click()

    def test_revenue_totals_filter_pending_and_historic_bill_details(self):
        from test_revenue import receipt
        a=receipt('good-1');b=receipt('good-2');b['seq']=2;b['event']['pool']='other'
        b['event']['merchant_pricing']['pool']='other'
        c=receipt('pending');c['seq']=3;c['event']['status']=499
        c['event']['billing']={'version':1,'state':'pending','reason':'request_incomplete','items':'invalid'}
        self.store.archive('usage','test-boot',[a,b,c])
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
        self.assertEqual(created['pools'][1], {'name':'claude-team','provider':'anthropic','deployments':[]})
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
        expect(self.page.locator('[name=pool]')).to_have_value('claude-team')
        self.assertEqual(self.page.locator('#dialog [name=provider], #dialog [name=multiplier]').count(), 0)
        self.page.locator('[name=id]').fill('claude-account')
        self.page.locator('[name=credential]').fill('FIXTURE_CLAUDE_KEY')
        self.page.locator('[name=proxy]').fill('https://proxy.example')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.selected-row td').nth(2)).to_have_text('1')
        self.assertEqual(len(self.router.writes), 3)
        added = self.router.writes[-1]
        self.assertEqual(added['pools'][1]['deployments'][0]['provider'], 'anthropic')
        self.assertEqual(added['pool_pricing'], [{'pool':'openai','multiplier_millionths':200000},
                                              {'pool':'claude-team','multiplier_millionths':250000}])
        self.assertEqual(added['pools'][0], created['pools'][0])
        self.page.locator('[data-action=edit-account][data-id=claude-account]').click()
        expect(self.page.locator('[name=pool] option')).to_have_count(1)
        expect(self.page.locator('[name=credential]')).to_have_value('FIXTURE_CLAUDE_KEY')

    def test_empty_state_requires_pool_before_accounts(self):
        empty = self.app._api('GET', '/api/state', None)
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
        self.page.locator('[name=id]').fill('new-oauth')
        self.page.locator('[name=pool]').select_option('openai')
        self.page.locator('[name=kind]').select_option('oauth')
        credential = {'provider': 'openai', 'proxy': 'socks5h://proxy.example:1080',
                      'credential': {'access_token': 'FIXTURE_ACCESS', 'refresh_token': 'FIXTURE_REFRESH'}}
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[0]['accounts']['new-oauth']['credential'], credential['credential'])
        self.page.locator('[data-action=edit-account][data-id=new-oauth]').click()
        expect(self.page.locator('[name=credential]')).to_have_value(json.dumps(
            {'provider':'openai','credential':credential['credential']},ensure_ascii=False,indent=2))
        expect(self.page.locator('[name=proxy]')).to_have_value('socks5h://proxy.example:1080')
        del credential['proxy']
        credential['credential']['access_token'] = 'ROTATED_FIXTURE_ACCESS'
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes), 2)
        self.assertEqual(self.router.writes[1]['accounts']['new-oauth'], credential)

    def test_offline_restart_displays_local_credentials_and_preserves_pending_changes(self):
        self.app.connected = False
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=account]').click()
        self.page.locator('[name=id]').fill('offline-key')
        self.page.locator('[name=pool]').select_option('openai')
        self.page.locator('[name=credential]').fill('LOCAL_PRIVATE_FIXTURE')
        self.page.locator('[name=proxy]').fill('https://user:FIXTURE_PASSWORD@proxy.example')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('.notice')).to_contain_text('等待同步')
        self.assertEqual(self.router.writes, [])
        self.page.reload()
        self.page.locator('aside [data-id=accounts]').click()
        self.page.locator('[data-action=edit-account][data-id=offline-key]').click()
        expect(self.page.locator('[name=credential]')).to_have_value('LOCAL_PRIVATE_FIXTURE')
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
        self.page.locator('[name=id]').fill('grok-native')
        self.page.locator('[name=kind]').select_option('oauth')
        self.page.locator('[name=credential]').fill(json.dumps(native))
        self.page.locator('[name=proxy]').fill('socks5h://proxy-1.example:1080')
        self.page.get_by_role('button', name='保存并同步').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[-1]['accounts']['grok-native']['credential'], expected)
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
        self.assertEqual(self.router.writes[-1]['accounts']['grok-batch']['credential'], expected)
        self.assertEqual(self.router.writes[-1]['accounts']['grok-batch']['proxy'], 'http://proxy-2.example:8080/')
        self.assertNotIn('PRIVATE_GROK', self.page.locator('body').inner_text())


if __name__ == '__main__':
    unittest.main()
