"""Local merchant console; the merchant owns its identity, TEE policy and credentials."""
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
from credential_protocol import PROTOCOL, access_only, fingerprint
from account_auth import AccountError, sync_error, verify as verify_account
from local_accounts import redact

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
        from platform_client import PlatformClient
        from platform_data import PlatformData
        self.platform_client = PlatformClient(self)
        self.platform_data = PlatformData(store)
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
        if clean_catalog and self.store.accounts.withdraw_empty_pools(router):
            return self.push_config()
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
                # A failed restore is retained locally, not retried every 10 seconds.
                self.accept(result, clean_catalog=False)
                self.push_config()
            else:
                self.accept(result, clean_catalog=False)
        else:
            self.accept(result, clean_catalog=False)
        self.sync_credentials()
        if self.store.accounts.withdraw_empty_pools(self.store.settings['router']):
            return self.push_config()
        return self.remote

    def check_accounts(self):
        router, book = self.store.settings['router'], self.store.accounts
        remote_accounts = {a['name']: a for a in (self.remote or {}).get('accounts', {}).get('data', [])}
        remote_ids = book.published_deployments(router)
        for identifier, account in book.sync_accounts(router).items():
            status = account['sync'].get('state')
            if status == 'paused':
                continue
            missing = not account['credential'] or ('api_key' in account['credential'] and not account['credential']['api_key'])
            if missing and identifier not in remote_ids:
                book.account_sync_result(router, identifier, account['basis'], 'paused', 'invalid_credential', '缺少本地账号凭证')
                continue
            if not account['enabled']:
                continue
            # A legacy remote account without a local credential cannot be
            # tested or renewed here. Do not withdraw a working remote account
            # merely because this installation has not imported its material.
            if missing:
                continue
            try:
                if status in ('checking', 'sending'):
                    raise AccountError('sync_uncertain')
                if account['provider'] != 'workbuddy' and account.get('oauth_account'):
                    token = access_only(account['credential'])
                    if token.get('expires_at', float('inf')) <= time.time():
                        raise AccountError('access_token_expired')
                    remote = remote_accounts.get(account['oauth_account'], {})
                    if (remote.get('access_sha256') == fingerprint(token)
                            and remote.get('health', {}).get('state') == 'reauthorization_required'):
                        raise AccountError('authentication_failed')
                if status in ('verified', 'synced'):
                    continue
                book.account_sync_result(router, identifier, account['basis'], 'checking')
                self.remote = book.view(router)
                verify_account(account)
                book.account_sync_result(router, identifier, account['basis'], 'verified')
            except Exception as error:
                if isinstance(error, (ValueError, TypeError, AttributeError)):
                    error = AccountError('invalid_credential')
                code, message = sync_error(error)
                book.account_sync_result(router, identifier, account['basis'], 'paused', code, message)
        self.remote = book.view(router)

    def sync_credentials(self):
        if self.remote and self.remote.get('merchant', {}).get('credential_protocol') == PROTOCOL:
            router, book = self.store.settings['router'], self.store.accounts
            self.renew_credentials()
            self.check_accounts()
            book.reconcile_accounts(router)
            pending = book.pending(router)
            if pending and not book.view(router)['local_sync']['error']:
                self.push_config()
            update = book.access_updates(router, self.remote)
            if update:
                for name, credential in update['accounts'].items():
                    rows = {i: a for i, a in book.sync_accounts(router).items() if a['oauth_account'] == name and a['enabled']}
                    for identifier, account in rows.items():
                        book.account_sync_result(router, identifier, account['basis'], 'sending')
                    try:
                        body = {'runtime_id': update['runtime_id'], 'accounts': {name: credential}}
                        result = self.transport.call('PUT', '/merchant/accounts/access', body)
                        self.accept(result, clean_catalog=False)
                        book.confirm_token_upload(router, body['accounts'], result)
                    except Exception as error:
                        code, message = sync_error(error)
                        for identifier, account in rows.items():
                            book.account_sync_result(router, identifier, account['basis'], 'paused', code, message)
                    else:
                        for identifier, account in rows.items():
                            book.account_sync_result(router, identifier, account['basis'], 'synced')
                self.remote = book.view(router)

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
                for identifier, account in self.store.accounts.sync_accounts(router).items():
                    if account['oauth_account'] == name:
                        code = 'authentication_failed' if getattr(error, 'permanent', False) else 'renewal_failed'
                        self.store.accounts.account_sync_result(router, identifier, account['basis'], 'paused', code, str(AccountError(code)))
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
        sending = {}
        previous = self.store.accounts.published_deployments(router)
        affected = {d['id'] for p in pending['pools'] for d in p['deployments']
                    if d.get('oauth_account') in pending.get('accounts', {}) or d.get('api_key_ref') in pending.get('secrets', {})
                    or redact(d) != redact(previous.get(d['id']))}
        try:
            if not self.connected:
                self.connect()
                pending = self.store.accounts.pending(router)
                if not pending:
                    return self.remote
            self.renew_credentials()
            self.check_accounts()
            pending = self.store.accounts.pending(router)
            projected = self.store.accounts.projected_update(router, pending)
            published = {d['id']: d for p in projected['pools'] for d in p['deployments']}
            previous = self.store.accounts.published_deployments(router)
            sending = {i: a for i, a in self.store.accounts.sync_accounts(router).items() if i in published
                       and (a['oauth_account'] in projected.get('accounts', {}) or
                            not a['oauth_account'] and a['sync'].get('state') == 'verified' or
                            redact(published[i]) != redact(previous.get(i)))}
            for identifier, account in sending.items():
                self.store.accounts.account_sync_result(router, identifier, account['basis'], 'sending')
            result = self.transport.call('PUT', '/merchant/config', projected)
            self.accept(result, clean_catalog=False)
            self.store.accounts.confirm_token_upload(router, projected.get('accounts', {}), result)
            self.store.accounts.acknowledge(router, result)
            for identifier, account in self.store.accounts.sync_accounts(router).items():
                if account['sync'].get('state') in ('verified', 'sending'):
                    self.store.accounts.account_sync_result(router, identifier, account['basis'], 'synced')
        except Exception as error:
            # The local write already committed. Retain it even if a request or its
            # acknowledgement failed; never silently replay credential updates.
            code, detail = sync_error(error)
            self.store.accounts.sync_failed(router, detail)
            for identifier, account in self.store.accounts.sync_accounts(router).items():
                if (identifier in affected or identifier in sending) and account['sync'].get('state') != 'paused':
                    self.store.accounts.account_sync_result(router, identifier, account['basis'], 'paused', code, detail)
        self.remote = self.store.accounts.view(router)
        return self.remote

    def api(self, method, path, body, identity=None):
        # Status remains responsive while a remote archive operation is waiting.
        if (method, path) == ('GET', '/api/state'):
            with self.store.lock:
                return self._api(method, path, body)
        if method == 'GET' and urlsplit(path).path in ('/api/revenue', '/api/revenue/record', '/api/records/usage', '/api/records/refusals'):
            with self.store.lock:
                if identity is not None and identity != self.store.enrollment()['id']:
                    raise ValueError('商户已切换，请重新打开页面')
            return self.platform_data.read(path)
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
                self.platform_data.reset()
                self.remote = self.store.accounts.view(self.store.settings['router'])
                self.connected = False
                self.last_error = ''
            return {'saved': True, 'merchant_id': self.store.enrollment()['id']}
        url = urlsplit(path)
        if (method, path) == ('GET', '/api/state'):
            return {'settings': self.store.public(), 'enrollment': self.store.enrollment(), 'credentials': self.store.credentials(), 'identity': self.store.platform_identity(), 'profiles': self.store.profiles(), 'connected': self.connected, 'remote': self.remote, 'error': self.last_error}
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
                self.platform_data.reset()
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
            router = self.store.settings['router']
            for identifier, account in self.store.accounts.sync_accounts(router).items():
                if account['sync'].get('state') == 'paused':
                    self.store.accounts.retry_account(router, identifier)
            return self.push_config()
        if (method, path) == ('POST', '/api/accounts/retry'):
            if not isinstance(body, dict) or set(body) != {'id'} or not isinstance(body['id'], str):
                raise ValueError('账号重试参数无效')
            router = self.store.settings['router']
            self.store.accounts.retry_account(router, body['id'])
            try:
                if not self.connected:
                    self.connect()
                else:
                    self.accept(self.transport.call('GET', '/merchant/state'), clean_catalog=False)
                    if self.store.accounts.pending(router):
                        self.push_config()
                    self.sync_credentials()
            except Exception as error:
                code, detail = sync_error(error)
                account = self.store.accounts.sync_accounts(router)[body['id']]
                self.store.accounts.account_sync_result(router, body['id'], account['basis'], 'paused', code, detail)
                self.remote = self.store.accounts.view(router)
            return self.remote
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
            pool = next((p for p in (self.remote or {}).get('pools', []) if p['name'] == body['pool']), None)
            if pool and not pool['deployments']:
                body = {**body, 'draft_pool': {'provider': pool['provider'], 'allowed_models': pool.get('allowed_models')}}
            return self.transport.call('POST', '/merchant/accounts/test', body)
        if method == 'GET' and path.startswith('/api/accounts/'):
            return self.store.accounts.account(self.store.settings['router'], unquote(path[len('/api/accounts/'):]))
        if (method, path) == ('POST', '/api/refresh'):
            return self.accept(self.transport.call('POST', '/merchant/models/refresh'), catalog_refreshed=True)
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


def handler(application, origin):
    from service import handler as service_handler
    return service_handler(application, origin)

def main():
    from service import main as service_main
    service_main()

if __name__ == '__main__':
    main()
