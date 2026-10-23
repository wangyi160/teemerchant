"""Local merchant console; the merchant owns its identity, TEE policy and archives."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit, unquote, parse_qs
import webbrowser
from store import Store
from transport import Transport
from credential_protocol import PROTOCOL, access_only, router_update

ROOT = Path(__file__).parent

class Application:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport or Transport(store)
        self.csrf = secrets.token_urlsafe(32)
        self.connected = False
        self.remote = store.accounts.view(store.settings['router'])
        self.last_error = ''
        self.sync_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.last_catalog_check = time.monotonic()
        from platform_archive import PlatformArchive
        from platform_client import PlatformClient
        self.platform_client = PlatformClient(self)
        self.archive = PlatformArchive(store)
        from oauth_service import OAuthService
        self.oauth = OAuthService(self)

    def accept(self, result, *, clean_catalog=True, catalog_refreshed=False):
        if result['merchant']['id'] != self.store.enrollment()['id']:
            raise RuntimeError('商户身份与本机身份不一致')
        revision = result['settings']['revision']
        if revision < self.store.settings['minimum_revision']:
            raise RuntimeError('Router 状态早于本机已确认版本，已拒绝回退')
        self.store.settings['minimum_revision'] = revision
        self.store.settings['credentials_saved'] = True
        self.store.save()
        self.store.accounts.observe(self.store.settings['router'], result)
        self.remote, self.connected = self.store.accounts.view(self.store.settings['router']), True
        self.last_catalog_check = time.monotonic()
        router = self.store.settings['router']
        if clean_catalog and self.store.accounts.prune_catalog(router, check_only=True):
            if not catalog_refreshed:
                # Confirm removals against a new fetch, including an empty catalog.
                # Cached evidence can become invalid after a credential rotation.
                return self.accept(self.transport.call('POST', '/merchant/models/refresh'), catalog_refreshed=True)
            if self.store.accounts.prune_catalog(router):
                return self.push_config()
        return self.remote

    def connect(self):
        owner = self.store.control_token().split('.', 1)[1]
        expected = hashlib.sha256(owner.encode()).hexdigest()
        bound = self.transport.call('POST', '/merchant/bind', {})
        if (bound.get('merchant_control_version') != 1 or bound.get('merchant_id') != self.store.enrollment()['id']
                or not secrets.compare_digest(bound.get('owner_hash', ''), expected)):
            raise RuntimeError('Router 尚未支持商户独立控制密钥，未发送账号资料')
        result = self.transport.call('POST', '/merchant/unlock', {'minimum_revision': self.store.settings['minimum_revision']})
        if result['merchant'].get('owner_hash') != expected:
            raise RuntimeError('商户控制密钥不匹配，未发送账号资料')
        if result['merchant'].get('credential_protocol') != PROTOCOL:
            raise RuntimeError('Router 尚未支持 access-token-only-v1 协议，未发送账号资料')
        if result['merchant'].get('configuration_required'):
            pending = self.store.accounts.restore_pending(self.store.settings['router'], result)
            if pending:
                result = self.transport.call('PUT', '/merchant/config', router_update(pending))
                self.store.accounts.acknowledge(self.store.settings['router'], result)
        result = self.accept(result, clean_catalog=False)
        self.sync_credentials()
        return result

    def sync_credentials(self):
        if self.remote and self.remote.get('merchant', {}).get('credential_protocol') == PROTOCOL:
            update = self.store.accounts.access_updates(self.store.settings['router'], self.remote)
            if update:
                self.accept(self.transport.call('PUT', '/merchant/accounts/access', update), clean_catalog=False)

    def renew_credentials(self):
        from oauth_service import refresh
        router = self.store.settings['router']
        cached = self.store.accounts.view(router)
        # During an upgrade an older Router may still own a rotating refresh
        # token. Begin merchant renewal only after this protocol was confirmed.
        if not cached or cached.get('merchant', {}).get('credential_protocol') != PROTOCOL:
            return
        for name, account, basis in self.store.accounts.renewal_candidates(router):
            self.store.accounts.renewal_started(router, name, basis)
            try:
                credential = refresh(account['provider'], account['credential'], account.get('proxy', ''))
            except (RuntimeError, ValueError) as error:
                self.store.accounts.renewal_finished(router, name, basis, error=error)
            else:
                # A failed local save deliberately leaves the durable 'refreshing'
                # marker. Never send or retry tokens that were not durably saved.
                self.store.accounts.renewal_finished(router, name, basis, credential=credential)
        self.remote = self.store.accounts.view(router)

    def push_config(self):
        router = self.store.settings['router']
        pending = self.store.accounts.pending(router)
        if not pending:
            return self.remote
        try:
            if not self.connected:
                self.connect()
                pending = self.store.accounts.pending(router)
                if not pending:
                    return self.remote
            result = self.transport.call('PUT', '/merchant/config', router_update(pending))
            self.accept(result, clean_catalog=False)
            self.store.accounts.acknowledge(router, result)
        except Exception as error:
            # The local write already committed. Retain it even if a request or its
            # acknowledgement failed; never silently replay credential updates.
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else '同步失败，Router 写入结果待确认；本地资料已保存'
            self.store.accounts.sync_failed(router, message)
        self.remote = self.store.accounts.view(router)
        return self.remote

    def sync(self):
        result = self.archive.sync()
        if result.get('busy'):
            raise RuntimeError('管理平台账单正在同步，请稍后刷新')
        return result

    def api(self, method, path, body, identity=None):
        # Status remains responsive while a remote archive operation is waiting.
        if (method, path) == ('GET', '/api/state'):
            with self.store.lock:
                return self._api(method, path, body)
        if (method, path) == ('POST', '/api/sync'):
            with self.store.lock:
                if identity is not None and identity != self.store.enrollment()['id']:
                    raise ValueError('当前商户已切换，请刷新页面后重新操作')
            return self.sync()
        with self.state_lock:
            if identity is not None and identity != self.store.enrollment()['id']:
                raise ValueError('当前商户已切换，请刷新页面后重新操作')
            return self._api(method, path, body)

    def _api(self, method, path, body):
        if (method, path) == ('POST', '/api/policy'):
            if not isinstance(body,dict) or set(body) != {'kind','policy'} or body['kind'] not in ('tdx','snp') or not isinstance(body['policy'],dict):
                raise ValueError('信任策略格式无效')
            raw=json.dumps(body['policy'],ensure_ascii=False,indent=2).encode()
            if len(raw)>2*1024*1024: raise ValueError('信任策略文件过大')
            directory=self.store.directory/'policies';directory.mkdir(exist_ok=True)
            destination=directory/(body['kind']+'-'+hashlib.sha256(raw).hexdigest()+'.json')
            with destination.open('wb') as file:
                file.write(raw);file.flush();os.fsync(file.fileno())
            return {'path':str(destination)}
        if (method, path) == ('GET', '/api/credentials'):
            return self.store.credentials(reveal=True)
        if (method, path) == ('GET', '/api/identity'):
            return self.platform_client.refresh_identity()
        if method == 'POST' and path in ('/api/credentials', '/api/merchant-profile'):
            with self.store.lock:
                if path == '/api/credentials':
                    self.store.import_credentials(body)
                else:
                    self.store.switch_profile(body)
                self.transport.close()
                self.archive.reset()
                self.remote = self.store.accounts.view(self.store.settings['router'])
                self.connected = False
                self.last_error = ''
            return {'saved': True, 'merchant_id': self.store.enrollment()['id']}
        url = urlsplit(path)
        if method == 'GET' and url.path == '/api/revenue':
            return self.archive.revenue(url.query)
        if method == 'GET' and url.path == '/api/revenue/record':
            values = parse_qs(url.query, keep_blank_values=True, max_num_fields=2)
            if set(values) != {'instance', 'seq'} or any(len(v) != 1 for v in values.values()):
                raise ValueError('收入记录标识无效')
            instance, seq = values['instance'][0], values['seq'][0]
            if len(instance) > 256 or not seq.isascii() or not seq.isdigit() or len(seq) > 19:
                raise ValueError('收入记录标识无效')
            return self.archive.detail(instance, int(seq))
        if (method, path) == ('GET', '/api/state'):
            return {'settings': self.store.public(), 'enrollment': self.store.enrollment(), 'credentials': self.store.credentials(), 'identity': self.store.platform_identity(), 'profiles': self.store.profiles(), 'connected': self.connected, 'remote': self.remote, 'error': self.last_error, 'archive': self.archive.status()}
        if (method, path) == ('PUT', '/api/platform'):
            from platform_archive import platform_url
            if not isinstance(body, dict) or set(body) != {'platform_url'}:
                raise ValueError('管理平台设置无效')
            value = platform_url(body['platform_url'])
            with self.store.lock:
                previous = self.store.settings.copy()
                self.store.settings['platform_url'] = value
                try:
                    self.store.save()
                except Exception:
                    self.store.settings = previous
                    raise
                self.archive.reset()
            return {'saved': True}
        if (method, path) == ('PUT', '/api/settings'):
            self.store.save_connection(body)
            self.transport.close()
            self.remote, self.connected = self.store.accounts.view(self.store.settings['router']), False
            return {'saved': True}
        if (method, path) == ('POST', '/api/verify'):
            return self.transport.call('VERIFY', '')
        if (method, path) == ('POST', '/api/connect'):
            if not self.store.credentials()['saved']:
                raise ValueError('请先登录已通过审核的商户账号')
            return self.connect()
        if (method, path) == ('GET', '/api/merchant'):
            return self.accept(self.transport.call('GET', '/merchant/state'))
        if (method, path) == ('PUT', '/api/config'):
            self.store.accounts.stage(self.store.settings['router'], body)
            self.remote = self.store.accounts.view(self.store.settings['router'])
            # Offline changes are still saved and editable. Explicitly retry once
            # through /api/config/sync when the user requests synchronization.
            return self.push_config() if self.connected else self.remote
        if (method, path) == ('POST', '/api/config/sync'):
            return self.push_config()
        if (method, path) == ('POST', '/api/accounts/test'):
            if not self.connected:
                raise ValueError('请先连接 Router 再测试账号')
            if (not isinstance(body, dict) or set(body) - {'pool','proxy','credential','api_key','model'}
                    or not isinstance(body.get('pool'), str) or not isinstance(body.get('proxy'), str)
                    or not body['proxy'] or len(json.dumps(body).encode()) > 128 * 1024
                    or ('credential' in body) == ('api_key' in body)):
                raise ValueError('账号测试参数无效')
            if 'credential' in body and 'api_key' not in body['credential']:
                body = {**body, 'credential': access_only(body['credential'])}
            return self.transport.call('POST', '/merchant/accounts/test', body)
        if method == 'GET' and path.startswith('/api/accounts/'):
            return self.store.accounts.account(self.store.settings['router'], unquote(path[len('/api/accounts/'):]))
        if (method, path) == ('POST', '/api/refresh'):
            return self.accept(self.transport.call('POST', '/merchant/models/refresh'), catalog_refreshed=True)
        if (method, path) == ('POST', '/api/sync'):
            return self.sync()
        if method == 'GET' and path in ('/api/records/usage', '/api/records/refusals'):
            return {'data': self.archive.records(path.rsplit('/', 1)[1])}
        raise ValueError('接口不存在')

    def background(self, stop):
        while not stop.wait(10):
            with self.state_lock:
                self.oauth.prune()
                try:
                    self.renew_credentials()
                    if not self.connected and self.store.credentials()['saved'] and self.store.settings['router']:
                        self.connect()
                except Exception:
                    self.last_error = '商户续期或连接失败，请检查账号状态与连接配置'
                if self.connected:
                    try:
                        self.sync_credentials()
                        if time.monotonic() - self.last_catalog_check >= 60:
                            self.accept(self.transport.call('GET', '/merchant/state'))
                            self.sync_credentials()
                        self.last_error = ''
                    except Exception as error:
                        self.last_error = str(error)
                        if any(code in self.last_error for code in ('merchant_locked', 'merchant_forbidden', 'merchant_runtime_changed')):
                            try:
                                self.connect()
                                self.last_error = ''
                            except Exception as reconnect_error:
                                self.last_error = str(reconnect_error)

    def archive_background(self, stop):
        from platform_archive import INTERVAL
        while not stop.wait(10):
            with self.store.lock:
                ready = self.store.credentials()['saved'] and bool(self.store.settings.get('platform_url'))
            if ready and time.monotonic() - self.archive.last_attempt >= INTERVAL:
                try:
                    self.archive.sync(stop)
                except Exception:
                    pass  # The archive channel retains its own error and cursor.

def handler(application, origin):
    from service import handler as service_handler
    return service_handler(application, origin)

def main():
    from service import main as service_main
    service_main()

if __name__ == '__main__':
    main()
