"""Optional UI integration tests: pip install playwright; Microsoft Edge required.

Uses an isolated local server and a fake TEE transport. Never touches live accounts.
"""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
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

    def test_edit_preserves_blank_secrets_and_reports_remote_save(self):
        self.page.locator('[data-action=edit-account]').click()
        expect(self.page.locator('[name=id]')).to_have_value('example-001')
        expect(self.page.locator('[name=credential]')).to_have_value('')
        expect(self.page.locator('[name=proxy]')).to_have_value('')
        self.page.locator('[name=max_concurrent]').fill('12')
        self.page.get_by_role('button', name='保存并提交').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('#toast')).to_contain_text('账号已提交至 Router')
        self.assertEqual(len(self.router.writes), 1)
        update = self.router.writes[0]
        self.assertEqual(update['accounts'], {})
        self.assertEqual(update['secrets'], {})
        self.assertEqual(update['pools'][0]['deployments'][0]['max_concurrent'], 12)
        self.assertEqual(update['pools'][0]['deployments'][0]['oauth_account'], 'credential-ref')
        self.assertEqual(update['pool_pricing'][0]['multiplier_millionths'], 200000)

    def test_batch_file_preview_duplicate_block_and_single_commit(self):
        self.page.locator('[data-action=import-accounts]').click()
        self.page.locator('[name=pool]').fill('openai')
        self.page.locator('[name=proxy]').fill('socks5h://user:PRIVATE_PROXY_PASSWORD@proxy.example:1080')
        rows = [{'id': 'new-001', 'credential': {'access_token': 'PRIVATE_OAUTH_TOKEN'}},
                {'id': 'new-002', 'api_key': 'PRIVATE_API_KEY'}]
        self.page.locator('#import-files').set_input_files([
            {'name': 'first.json', 'mimeType': 'application/json', 'buffer': json.dumps(rows[0]).encode()},
            {'name': 'second.json', 'mimeType': 'application/json', 'buffer': json.dumps(rows[1]).encode()}])
        self.page.wait_for_function("document.querySelector('[name=entries]').value.includes('new-002')")
        self.page.get_by_role('button', name='校验并预览').click()
        expect(self.page.locator('#import-review')).to_contain_text('2 个账号校验通过')
        self.assertNotIn('PRIVATE_', self.page.locator('#import-review').inner_text())
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
        self.assertNotIn('PRIVATE_', self.page.locator('body').inner_text())
        self.assertEqual(self.page.locator('#dialog textarea').count(), 0)

    def test_revision_conflict_keeps_dialog_open_and_does_not_claim_success(self):
        self.router.reject = True
        self.page.locator('[data-action=edit-account]').click()
        self.page.locator('[name=max_concurrent]').fill('12')
        self.page.get_by_role('button', name='保存并提交').click()
        expect(self.page.locator('#dialog .error')).to_contain_text('配置已变更')
        expect(self.page.get_by_role('button', name='保存并提交')).to_be_enabled()
        self.assertEqual(self.router.writes, [])
        self.page.locator('[data-action=close]').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()

    def test_add_oauth_then_rotate_credentials_without_replacing_proxy(self):
        self.page.locator('[data-action=account]').click()
        self.page.locator('[name=id]').fill('new-oauth')
        self.page.locator('[name=pool]').fill('openai')
        self.page.locator('[name=kind]').select_option('oauth')
        credential = {'provider': 'openai', 'proxy': 'socks5h://proxy.example:1080',
                      'credential': {'access_token': 'FIXTURE_ACCESS', 'refresh_token': 'FIXTURE_REFRESH'}}
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.page.get_by_role('button', name='保存并提交').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(self.router.writes[0]['accounts']['new-oauth']['credential'], credential['credential'])
        self.page.locator('[data-action=edit-account][data-id=new-oauth]').click()
        expect(self.page.locator('[name=credential]')).to_have_value('')
        del credential['proxy']
        credential['credential']['access_token'] = 'ROTATED_FIXTURE_ACCESS'
        self.page.locator('[name=credential]').fill(json.dumps(credential))
        self.page.get_by_role('button', name='保存并提交').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assertEqual(len(self.router.writes), 2)
        self.assertEqual(self.router.writes[1]['accounts']['new-oauth'], credential)


if __name__ == '__main__':
    unittest.main()
