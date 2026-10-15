"""Merchant-owned account book, encrypted at rest before any Router write."""
import copy
import json
import os
import re
import threading
import time


def config_from(remote):
    settings = remote['settings']
    return {'expected_revision': settings['revision'], 'pools': copy.deepcopy(remote.get('pools', [])),
            'customers': [], 'customer_defaults': copy.deepcopy(settings.get('customer_defaults')),
            'pool_pricing': copy.deepcopy(settings.get('pool_pricing', [])),
            'max_concurrent': settings.get('max_concurrent', 16),
            'rpm': settings.get('rpm', 0), 'tpm': settings.get('tpm', 0)}


def deployments(config):
    return [d for p in (config or {}).get('pools', []) for d in p['deployments']]


def redact(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if k == 'proxy' and v else redact(v))
                for k, v in value.items() if k not in ('credential', 'secrets')}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class AccountBook:
    def __init__(self, directory, identity, protect):
        self.path = directory / 'accounts.dpapi'
        self.identity, self.protect = identity, protect
        self.lock = threading.RLock()
        self.data = json.loads(protect(self.path.read_bytes(), True)) if self.path.exists() else {
            'version': 1, 'identity': identity, 'routers': {}}
        if self.data.get('version') != 1 or self.data.get('identity') != identity:
            raise RuntimeError('本地账号库身份或格式不匹配')

    def _write(self, data):
        temporary = self.path.with_suffix('.tmp')
        encrypted = self.protect(json.dumps(data, ensure_ascii=False).encode())
        with temporary.open('wb') as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        self.data = data

    def _book(self, data, router):
        return data['routers'].setdefault(router, {'remote': None, 'config': None, 'accounts': {}, 'secrets': {},
            'pending': None, 'local_revision': 0, 'sync_state': 'synced', 'sync_error': '', 'saved_at': None})

    def observe(self, router, remote):
        """Cache public remote state; never overwrite local unsynchronized changes."""
        if remote['merchant']['id'] != self.identity:
            raise RuntimeError('商户身份与本地账号库不一致')
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            previous = book['remote']
            if (previous and book['config'] is not None and not book['pending']
                    and previous['settings']['revision'] != remote['settings']['revision']):
                book['sync_state'] = 'remote_changed'
            book['remote'] = copy.deepcopy(remote)
            if book['config'] is None:
                book['config'] = config_from(remote)
            self._write(data)

    def stage(self, router, body):
        """Persist material and the write intent together. Failure prevents transmission."""
        if not isinstance(body, dict) or not isinstance(body.get('pools'), list):
            raise ValueError('账号配置格式无效')
        if type(body.get('expected_revision')) is not int:
            raise ValueError('缺少 Router 配置版本')
        for field in ('customers', 'pool_pricing'):
            if not isinstance(body.get(field), list):
                raise ValueError('商户配置格式无效')
        for field in ('max_concurrent', 'rpm', 'tpm'):
            if type(body.get(field)) is not int or body[field] < (1 if field == 'max_concurrent' else 0):
                raise ValueError('商户限额格式无效')
        def valid_name(value):
            return isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', value)
        ids = set()
        for pool in body['pools']:
            if not isinstance(pool, dict) or not valid_name(pool.get('name')) or not isinstance(pool.get('deployments'), list):
                raise ValueError('账号池格式无效')
            allowed = pool.get('allowed_models')
            if allowed is not None and (not isinstance(allowed, list) or len(allowed) > 4096
                    or any(not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9._:/-]{1,256}', model)
                           or any(part in ('', '.', '..') for part in model.split('/')) for model in allowed)
                    or len(set(allowed)) != len(allowed)):
                raise ValueError('对外模型列表必须是有效且不重复的模型 ID')
            for account in pool['deployments']:
                if (not isinstance(account, dict) or not valid_name(account.get('id'))
                        or account['id'] in ids or account.get('provider') not in ('openai','anthropic','xai','workbuddy')):
                    raise ValueError('账号格式无效或账号 ID 重复')
                ids.add(account['id'])
        for field in ('accounts', 'secrets'):
            if not isinstance(body.get(field, {}), dict):
                raise ValueError('账号凭证格式无效')
            if any(not valid_name(key) for key in body.get(field, {})):
                raise ValueError('账号引用无效')
            for value in body.get('accounts', {}).values():
                if not isinstance(value, dict) or ('credential' in value and not isinstance(value['credential'], dict)):
                    raise ValueError('OAuth 凭证须为对象')
            if not isinstance(body.get('delete_'+field, []), list) or any(not valid_name(key) for key in body.get('delete_'+field, [])):
                raise ValueError('删除账号引用无效')
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            if body.get('_local_revision', book['local_revision']) != book['local_revision']:
                raise ValueError('local_revision_conflict：本地配置已变更，请刷新后重试')
            if not book['remote']:
                raise ValueError('请先连接一次 Router 以建立商户配置')
            if body['expected_revision'] != book['remote']['settings']['revision']:
                raise ValueError('revision_conflict：Router 配置版本已变更，请刷新后重试')
            if book['pending'] and '_local_revision' not in body:
                raise ValueError('local_revision_conflict：本地有待同步修改，请刷新后重试')
            update = copy.deepcopy(body)
            update.pop('_local_revision', None)
            old_deployments = {d['id']: d for d in deployments(book['config'])}
            for d in deployments(update):
                if d.get('proxy') == '[redacted]':
                    d['proxy'] = old_deployments.get(d['id'], {}).get('proxy', '[redacted]')
            pending = book['pending'] or {}
            for field in ('accounts', 'secrets'):
                merged = copy.deepcopy(pending.get(field, {}))
                for key, value in update.get(field, {}).items():
                    if field == 'accounts':
                        if not isinstance(value, dict):
                            raise ValueError('账号凭证格式无效')
                        merged[key] = {**merged.get(key, {}), **copy.deepcopy(value)}
                        book['accounts'][key] = {**book['accounts'].get(key, {}), **copy.deepcopy(value)}
                    else:
                        if not isinstance(value, str):
                            raise ValueError('API key 格式无效')
                        merged[key] = value
                        book['secrets'][key] = value
                deleted = set(pending.get('delete_' + field, [])) | set(update.get('delete_' + field, []))
                deleted -= set(update.get(field, {}))
                for key in deleted:
                    merged.pop(key, None)
                    book[field].pop(key, None)
                update[field] = merged
                update['delete_' + field] = sorted(deleted)
            book['config'] = {k: copy.deepcopy(v) for k, v in update.items()
                              if k not in ('accounts', 'secrets', 'delete_accounts', 'delete_secrets')}
            book['pending'] = update
            book['local_revision'] += 1
            book['sync_state'], book['sync_error'] = 'pending', ''
            book['saved_at'] = int(time.time())
            self._write(data)

    def pending(self, router):
        with self.lock:
            return copy.deepcopy(self.data['routers'].get(router, {}).get('pending'))

    def sync_failed(self, router, message):
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            book['sync_state'], book['sync_error'] = 'pending', message
            self._write(data)

    def acknowledge(self, router, remote):
        if remote['merchant']['id'] != self.identity:
            raise RuntimeError('商户身份与本地账号库不一致')
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            previous = {d['id']: d for d in deployments(book['config'])}
            config = config_from(remote)
            for d in deployments(config):
                if d.get('proxy') == '[redacted]':
                    d['proxy'] = previous.get(d['id'], {}).get('proxy', '[redacted]')
            book.update(remote=copy.deepcopy(remote), config=config, pending=None,
                        sync_state='synced', sync_error='')
            self._write(data)

    def view(self, router):
        with self.lock:
            book = self.data['routers'].get(router)
            if not book or not book['remote']:
                return None
            view = redact(copy.deepcopy(book['remote']))
            config = book['config']
            view['pools'] = redact(config['pools'])
            for field in ('pool_pricing', 'customer_defaults', 'max_concurrent', 'rpm', 'tpm'):
                view['settings'][field] = copy.deepcopy(config.get(field))
            # The Router revision guards writes; local_revision guards offline edits.
            view['local_revision'] = book['local_revision']
            missing = [d['id'] for d in deployments(config) if not self._material(book, d)['credential_present']]
            view['local_sync'] = {'state': book['sync_state'], 'error': book['sync_error'],
                                  'saved_at': book['saved_at'], 'missing_credentials': missing}
            return view

    @staticmethod
    def _material(book, deployment):
        d = deployment
        account = book['accounts'].get(d.get('oauth_account'), {})
        credential = account.get('credential') if d.get('oauth_account') else book['secrets'].get(d.get('api_key_ref'))
        proxy = account.get('proxy', '') if d.get('oauth_account') else d.get('proxy', '')
        if proxy == '[redacted]':
            proxy = ''
        kind = 'oauth' if d.get('oauth_account') and d['provider'] != 'workbuddy' else 'api'
        if kind == 'api' and isinstance(credential, dict):
            credential = credential.get('api_key')
        raw = (json.dumps({'provider': d['provider'], 'credential': credential}, ensure_ascii=False, indent=2)
               if kind == 'oauth' and credential else credential or '')
        return {'id': d['id'], 'credential_text': raw, 'credential_present': bool(credential),
                'proxy': proxy, 'kind': kind, 'provider': d['provider']}

    def account(self, router, identifier):
        with self.lock:
            book = self.data['routers'].get(router, {})
            d = next((d for d in deployments(book.get('config')) if d['id'] == identifier), None)
            if not d:
                raise ValueError('本地账号不存在')
            return {**self._material(book, d), 'local_revision': book['local_revision']}

    def import_recovery(self, router, bundle):
        """Recover only credentials referenced by this merchant's existing local pools."""
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            if book['pending']:
                raise RuntimeError('有待同步修改，不能覆盖本地账号库')
            recovered = 0
            originals = {d['id']: d for d in deployments({'pools': bundle['config']['models']})}
            for d in deployments(book['config']):
                source = originals.get(d['id'])
                if not source or source['provider'] != d['provider']:
                    raise ValueError('恢复资料与本地账号不匹配')
                ref = d.get('oauth_account')
                if ref:
                    value = bundle.get('accounts', {}).get(ref)
                    if value and value['provider'] == d['provider']:
                        book['accounts'][ref] = copy.deepcopy(value)
                        recovered += 1
                else:
                    ref = d.get('api_key_ref')
                    if ref in bundle.get('secrets', {}) and not ref.startswith('merchant_internal_'):
                        book['secrets'][ref] = bundle['secrets'][ref]
                        d['proxy'] = source.get('proxy')
                        recovered += 1
            book['local_revision'] += 1
            book['saved_at'] = int(time.time())
            self._write(data)
            return recovered
