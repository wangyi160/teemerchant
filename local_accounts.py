"""Merchant-owned account book, encrypted at rest before any Router write."""
import copy
import hashlib
import json
import os
import re
import threading
import time
from credential_protocol import access_only, normalized, fingerprint


def config_from(remote):
    settings = remote['settings']
    return {'expected_revision': settings['revision'], 'pools': copy.deepcopy(remote.get('pools', [])),
            'customers': [], 'customer_defaults': copy.deepcopy(settings.get('customer_defaults')),
            'pool_pricing': copy.deepcopy(settings.get('pool_pricing', [])),
            'max_concurrent': settings.get('max_concurrent', 16),
            'rpm': settings.get('rpm', 0), 'tpm': settings.get('tpm', 0),
            'compatibility': copy.deepcopy(remote.get('compatibility', {})),
            'restrictions': copy.deepcopy(remote.get('restrictions', []))}


def deployments(config):
    return [d for p in (config or {}).get('pools', []) for d in p['deployments']]


def redact(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if k == 'proxy' and v else redact(v))
                for k, v in value.items() if k not in ('credential', 'secrets')}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def current_pool_models(remote, pool, now):
    """Return successful official evidence, or None when unavailable/uncertain."""
    enabled = {d['id'] for d in pool['deployments'] if d.get('enabled', True)}
    rows = [r for r in remote.get('catalog', {}).get('data', [])
            if r.get('pool') == pool['name'] and r.get('deployment') in enabled]
    if not enabled or {r.get('deployment') for r in rows} != enabled:
        return None
    snapshots = []
    for row in rows:
        status, models = row.get('catalog') or {}, row.get('official_models')
        fetched, interval = status.get('fetched_at'), status.get('refresh_secs')
        providers = {d['provider'] for d in pool['deployments'] if d.get('enabled', True)}
        scope_ok = ((status.get('scope') == 'pool' and status.get('source_deployment') in enabled)
                    or (status.get('scope') == 'provider' and len(providers) == 1
                        and status.get('provider') in providers - {'workbuddy'}))
        if (not scope_ok or status.get('complete_catalog') is not True
                or status.get('refreshing') is not False or 'last_error' not in status
                or status['last_error'] is not None
                or type(fetched) is not int or type(interval) is not int
                or not 0 <= now - fetched < interval or not isinstance(models, dict)
                or type(status.get('models')) is not int or status['models'] != len(models)):
            return None
        snapshots.append(set(models))
    # Rows for accounts sharing one pool must agree, including during refresh.
    return snapshots[0] if all(ids == snapshots[0] for ids in snapshots) else None


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
            overrides = {c['user_id']: c for c in book.get('recovery_customers',
                         (previous or {}).get('settings', {}).get('customers', []))}
            overrides.update({c['user_id']: copy.deepcopy(c) for c in remote['settings'].get('customers', [])})
            book['recovery_customers'] = list(overrides.values())
            if (previous and book['config'] is not None and not book['pending']
                    and previous['settings']['revision'] != remote['settings']['revision']):
                book['sync_state'] = 'remote_changed'
            book['remote'] = copy.deepcopy(remote)
            if book['config'] is None:
                book['config'] = config_from(remote)
            elif not remote['merchant'].get('configuration_required'):
                # Published prices are authoritative. A failed edit must never
                # survive as a local override or be replayed with account changes.
                published = {p['name'] for p in remote.get('pools', [])}
                prices = remote['settings'].get('pool_pricing', [])
                for config in (book['config'], book['pending']):
                    if config is not None:
                        previous_prices = config.get('pool_pricing', [])
                        retained = {p['name'] for p in config['pools']}
                        confirmed = [copy.deepcopy(p) for p in prices if p['pool'] in retained]
                        confirmed.extend(copy.deepcopy(p) for p in previous_prices if p['pool'] not in published)
                        if confirmed != previous_prices:
                            config['pool_pricing'] = confirmed
                            book['local_revision'] += 1
            self._write(data)

    def guard_pricing(self, router, body):
        """Account writes cannot smuggle an unconfirmed published price change."""
        with self.lock:
            book = self.data['routers'].get(router, {})
            published = {p['name'] for p in (book.get('remote') or {}).get('pools', [])}
            retained = {p['name'] for p in body.get('pools', []) if p.get('deployments')}
            previous = {p['pool']: p for p in (book.get('remote') or {}).get('settings', {}).get('pool_pricing', [])}
            proposed = {p['pool']: p for p in body.get('pool_pricing', [])}
            if any(previous.get(name) != proposed.get(name) for name in published & retained):
                raise ValueError('倍率未修改，请在编辑账号池中单独保存倍率')

    def confirm_price(self, router, remote, pool, multiplier, previous_revision):
        """Commit only this confirmed edit; leave account drafts and secrets alone."""
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            for config in (book['config'], book['pending']):
                if config is not None:
                    config['pool_pricing'] = [p for p in config.get('pool_pricing', []) if p['pool'] != pool]
                    config['pool_pricing'].append({'pool': pool, 'multiplier_millionths': multiplier})
                    if (config is book['config'] or config['expected_revision'] == previous_revision
                            and remote['settings']['revision'] == previous_revision + 1):
                        config['expected_revision'] = remote['settings']['revision']
            book['local_revision'] += 1
            if not book['pending']:
                book['sync_state'], book['sync_error'] = 'synced', ''
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
        ids, names = set(), set()
        for pool in body['pools']:
            if not isinstance(pool, dict) or not valid_name(pool.get('name')) or not isinstance(pool.get('deployments'), list):
                raise ValueError('账号池格式无效')
            if pool['name'] in names:
                raise ValueError('账号池名称重复')
            names.add(pool['name'])
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
            for field in ('compatibility', 'restrictions'):
                if field not in update and field in (book['config'] or {}):
                    update[field] = copy.deepcopy(book['config'][field])
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
                    if field == 'accounts':
                        book.get('token_sync', {}).pop(key, None)
                update[field] = merged
                update['delete_' + field] = sorted(deleted)
            book['config'] = {k: copy.deepcopy(v) for k, v in update.items()
                              if k not in ('accounts', 'secrets', 'delete_accounts', 'delete_secrets')}
            book['account_sync'] = {k: v for k, v in book.get('account_sync', {}).items() if k in ids}
            book['pending'] = update
            book['local_revision'] += 1
            book['sync_state'], book['sync_error'] = 'pending', ''
            book['saved_at'] = int(time.time())
            self._write(data)

    def pending(self, router):
        with self.lock:
            return copy.deepcopy(self.data['routers'].get(router, {}).get('pending'))

    @staticmethod
    def _sync_accounts(book):
        result = {}
        remote_accounts = {a['name']: a for a in (book.get('remote') or {}).get('accounts', {}).get('data', [])}
        for d in deployments(book.get('config')):
            account = book.get('accounts', {}).get(d.get('oauth_account'), {})
            value = {'provider': d['provider'], 'credential': copy.deepcopy(account.get('credential', {})),
                     'proxy': account.get('proxy', d.get('proxy', '')),
                     'enabled': d.get('enabled', True) and account.get('enabled', remote_accounts.get(d.get('oauth_account'), {}).get('enabled', True)),
                     'oauth_account': d.get('oauth_account')}
            if not d.get('oauth_account'):
                value['credential'] = {'api_key': book.get('secrets', {}).get(d.get('api_key_ref'))}
            value['basis'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            state = book.get('account_sync', {}).get(d['id'], {})
            value['sync'] = copy.deepcopy(state) if state.get('basis') == value['basis'] else {}
            result[d['id']] = value
        return result

    def sync_accounts(self, router):
        with self.lock:
            return self._sync_accounts(self.data['routers'].get(router, {}))

    def published_deployments(self, router):
        with self.lock:
            remote = self.data['routers'].get(router, {}).get('remote')
            return {d['id']: copy.deepcopy(d) for d in deployments(remote)}

    def account_sync_result(self, router, identifier, basis, state, code='', message=''):
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            account = self._sync_accounts(book).get(identifier)
            if not account or account['basis'] != basis:
                raise RuntimeError('账号已变更，请重新检查')
            book.setdefault('account_sync', {})[identifier] = {
                'basis': basis, 'state': state, 'code': code, 'error': message, 'updated_at': int(time.time())}
            self._write(data)

    def confirm_token_upload(self, router, sent, remote):
        """Record only a token actually sent and acknowledged by this Router runtime."""
        if remote.get('merchant', {}).get('id') != self.identity:
            raise RuntimeError('商户身份与本地账号库不一致')
        runtime = remote.get('merchant', {}).get('runtime_id')
        if not isinstance(runtime, str) or not runtime:
            return
        statuses = {a['name']: a for a in remote.get('accounts', {}).get('data', [])}
        confirmed = {}
        now = int(time.time())
        for name, update in sent.items():
            credential = update.get('credential', {})
            if not credential.get('access_token'):
                continue
            digest = fingerprint(credential)
            if statuses.get(name, {}).get('access_sha256') == digest:
                confirmed[name] = {'last_success_at': now, 'access_sha256': digest, 'runtime_id': runtime}
        if confirmed:
            with self.lock:
                data = copy.deepcopy(self.data)
                self._book(data, router).setdefault('token_sync', {}).update(confirmed)
                self._write(data)

    def retry_account(self, router, identifier):
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            if identifier not in self._sync_accounts(book):
                raise ValueError('本地账号不存在')
            book.setdefault('account_sync', {}).pop(identifier, None)
            ref = self._sync_accounts(book)[identifier]['oauth_account']
            renewal = book.get('renewals', {}).get(ref, {})
            if renewal.get('state') == 'retry':
                renewal['retry_at'] = 0
            self._write(data)

    def projected_update(self, router, update):
        """Paused material stays local, including after a fresh Router runtime."""
        from credential_protocol import router_update
        with self.lock:
            result = copy.deepcopy(update)
            paused = {name for name, account in self.sync_accounts(router).items()
                      if account['sync'].get('state') in ('paused', 'checking', 'sending')}
            for pool in result.get('pools', []):
                pool['deployments'] = [d for d in pool['deployments'] if d['id'] not in paused]
            refs = {d.get('oauth_account') for d in deployments(result)}
            keys = {d.get('api_key_ref') for d in deployments(result)}
            result['accounts'] = {k: v for k, v in result.get('accounts', {}).items() if k in refs}
            result['secrets'] = {k: v for k, v in result.get('secrets', {}).items() if k in keys}
            book = self.data['routers'].get(router, {})
            known = {a['name'] for a in (book.get('remote') or {}).get('accounts', {}).get('data', [])}
            for ref in refs - known - {None}:
                if ref in book.get('accounts', {}):
                    result['accounts'][ref] = copy.deepcopy(book['accounts'][ref])
            return router_update(result)

    def reconcile_accounts(self, router):
        """Publish healthy local accounts / withdraw held accounts once, not each tick."""
        with self.lock:
            book = self.data['routers'].get(router, {})
            if not book.get('config') or not book.get('remote') or book.get('pending'):
                return False
            projected = self.projected_update(router, book['config'])
            if {d['id'] for d in deployments(projected)} == {d['id'] for d in deployments(book['remote'])}:
                return False
            update = copy.deepcopy(book['config'])
            update.update(expected_revision=book['remote']['settings']['revision'], _local_revision=book['local_revision'])
            refs = {d.get('oauth_account') for d in deployments(update)}
            keys = {d.get('api_key_ref') for d in deployments(update)}
            update['accounts'] = {k: copy.deepcopy(v) for k, v in book['accounts'].items() if k in refs}
            update['secrets'] = {k: v for k, v in book['secrets'].items() if k in keys}
            self.stage(router, update)
            return True

    def renewal_candidates(self, router, now=None):
        """A durable 'refreshing' marker is never blindly retried after a crash."""
        now = time.time() if now is None else now
        with self.lock:
            book = self.data['routers'].get(router, {})
            refs = {d.get('oauth_account') for d in deployments(book.get('config')) if d.get('enabled', True)}
            paused = {a['oauth_account'] for a in self._sync_accounts(book).values()
                      if a['sync'].get('state') in ('paused', 'checking', 'sending')}
            result = []
            for name in refs:
                account = book.get('accounts', {}).get(name)
                if name in paused or not account or not account.get('enabled', True) or account.get('provider') == 'workbuddy': continue
                try:
                    credential = normalized(account.get('credential', {}))
                    if not credential.get('refresh_token'): continue
                    expiry = access_only(credential).get('expires_at')
                except (ValueError, TypeError, AttributeError):
                    continue  # The per-account authentication check reports this.
                if expiry is None or expiry > now + 300: continue
                basis = hashlib.sha256(json.dumps(account['credential'], sort_keys=True).encode()).hexdigest()
                status = book.get('renewals', {}).get(name, {})
                if status.get('basis') == basis and (status.get('state') in ('refreshing','reauthorization_required','uncertain') or status.get('retry_at',0)>now): continue
                result.append((name, copy.deepcopy(account), basis))
            return result

    def renewal_started(self, router, name, basis):
        with self.lock:
            data = copy.deepcopy(self.data);book=self._book(data,router)
            book.setdefault('renewals',{})[name]={'basis':basis,'state':'refreshing'}
            self._write(data)

    def renewal_finished(self, router, name, basis, credential=None, error=None):
        with self.lock:
            data=copy.deepcopy(self.data);book=self._book(data,router)
            account=book['accounts'].get(name)
            if not account or hashlib.sha256(json.dumps(account['credential'],sort_keys=True).encode()).hexdigest()!=basis:
                raise RuntimeError('续期期间账号凭证发生变化')
            if credential is not None:
                account['credential']=copy.deepcopy(credential)
                # Any already-staged configuration must carry the newest locally
                # persisted credentials too; never overwrite a rotated token.
                if book['pending'] and name in book['pending'].get('accounts',{}):
                    book['pending']['accounts'][name]['credential']=copy.deepcopy(credential)
                book.setdefault('renewals',{})[name]={'state':'active','renewed_at':int(time.time()),'access_sha256':fingerprint(credential)}
                book['local_revision']+=1;book['saved_at']=int(time.time())
            else:
                status='uncertain' if getattr(error,'uncertain',False) else 'reauthorization_required' if getattr(error,'permanent',False) else 'retry'
                book.setdefault('renewals',{})[name]={'basis':basis,'state':status,'error':str(error),'retry_at':int(time.time())+60}
            self._write(data)

    def access_updates(self, router, remote):
        with self.lock:
            book=self.data['routers'].get(router,{})
            pending=book.get('pending') or {}
            enabled_refs={d.get('oauth_account') for d in deployments(book.get('config')) if d.get('enabled',True)}
            statuses={a['name']:a for a in remote.get('accounts',{}).get('data',[])}
            paused={a['oauth_account'] for a in self._sync_accounts(book).values()
                    if a['sync'].get('state') in ('paused','checking','sending')}
            updates={}
            for name,account in book.get('accounts',{}).items():
                status=statuses.get(name)
                if not status or name in paused or name not in enabled_refs or not account.get('enabled',True) or account.get('provider')=='workbuddy': continue
                try: credential=access_only(account['credential'])
                except (ValueError, TypeError, AttributeError): continue
                if pending:
                    patch=pending.get('accounts',{}).get(name,{})
                    if name not in enabled_refs or 'proxy' in patch: continue
                    # Unrelated pending edits must not block automatic renewal.
                    # A manual credential/proxy edit still follows its explicit
                    # configuration save, rather than being partially applied.
                    if 'credential' in patch and book.get('renewals',{}).get(name,{}).get('access_sha256')!=fingerprint(credential): continue
                receipt = book.get('token_sync', {}).get(name, {})
                if (status.get('access_sha256') == fingerprint(credential)
                        and receipt.get('access_sha256') == fingerprint(credential)
                        and receipt.get('runtime_id') == remote['merchant'].get('runtime_id')):
                    continue
                if not isinstance(status.get('access_sha256'),str): continue
                updates[name]={'expected_access_sha256':status['access_sha256'],'credential':credential}
            if not updates: return None
            return {'runtime_id':remote['merchant']['runtime_id'],'accounts':updates}

    def restore_pending(self, router, remote):
        """Rebuild a fresh TEE runtime using only merchant-local durable material."""
        if remote['merchant']['id'] != self.identity or not remote['merchant'].get('configuration_required'):
            raise ValueError('Router 不需要恢复商户配置')
        with self.lock:
            book = self.data['routers'].get(router)
            if not book or book['config'] is None:
                return None
            update = copy.deepcopy(book['pending'] or book['config'])
            update['expected_revision'] = remote['settings']['revision']
            update['_local_revision'] = book['local_revision']
            if not update.get('customers'):
                update['customers'] = copy.deepcopy(book.get('recovery_customers',
                    (book['remote'] or {}).get('settings', {}).get('customers', [])))
            rows = deployments(update)
            account_refs = {d['oauth_account'] for d in rows if d.get('oauth_account')}
            secret_refs = {d['api_key_ref'] for d in rows if d.get('api_key_ref')}
            # Missing material is held per account by the authentication pass;
            # it must not prevent the other accounts from restoring service.
            update['accounts'] = {k: copy.deepcopy(book['accounts'][k]) for k in account_refs if k in book['accounts']}
            update['secrets'] = {k: book['secrets'][k] for k in secret_refs if k in book['secrets']}
            update['delete_accounts'], update['delete_secrets'] = [], []
            self.observe(router, remote)
            self.stage(router, update)
            return self.pending(router)

    def prune_catalog(self, router, *, check_only=False):
        """Durably stage policy removals without replaying unrelated pending edits."""
        with self.lock:
            book = self.data['routers'].get(router)
            if (not book or not book['remote'] or not book['config'] or book['pending']
                    or book['sync_state'] != 'synced'):
                return False
            remote = book['remote']
            if book['config']['expected_revision'] != remote['settings']['revision']:
                return False
            update, changed = copy.deepcopy(book['config']), False
            for pool in update['pools']:
                allowed = pool.get('allowed_models')
                if not allowed:  # Preserve both all-model mode and explicit deny-all.
                    continue
                models = current_pool_models(remote, pool, time.time())
                if models is None:
                    continue
                kept = [model for model in allowed if model in models]
                if kept != allowed:
                    pool['allowed_models'], changed = kept, True
            if not changed:
                return False
            if check_only:
                return True
            update['_local_revision'] = book['local_revision']
            self.stage(router, update)
            return True

    def sync_failed(self, router, message):
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            book['sync_state'], book['sync_error'] = 'pending', message
            self._write(data)

    def withdraw_empty_pools(self, router):
        """Upgrade old published empty pools without replaying pending edits."""
        with self.lock:
            book = self.data['routers'].get(router)
            if not book or book['pending'] or book['sync_state'] != 'synced':
                return False
            if not any(not pool['deployments'] for pool in book['remote'].get('pools', [])):
                return False
            update = copy.deepcopy(book['config'])
            update['expected_revision'] = book['remote']['settings']['revision']
            update['_local_revision'] = book['local_revision']
            self.stage(router, update)
            return True

    def acknowledge(self, router, remote):
        if remote['merchant']['id'] != self.identity:
            raise RuntimeError('商户身份与本地账号库不一致')
        with self.lock:
            data = copy.deepcopy(self.data)
            book = self._book(data, router)
            previous = {d['id']: d for d in deployments(book['config'])}
            config = config_from(remote)
            # Router acknowledges only published pools. Retain local empty
            # drafts, their order, model selections and multiplier after sync.
            pools = {pool['name']: pool for pool in config['pools']}
            empty = set()
            paused = {name for name, account in self._sync_accounts(book).items()
                      if account['sync'].get('state') in ('paused', 'checking', 'sending')}
            for pool in book['config']['pools']:
                held = [d for d in pool['deployments'] if d['id'] in paused]
                if (not pool['deployments'] or held) and pool['name'] not in pools:
                    pools[pool['name']] = copy.deepcopy(pool)
                    empty.add(pool['name'])
                elif held:
                    published = {d['id']: d for d in pools[pool['name']]['deployments']}
                    published.update({d['id']: copy.deepcopy(d) for d in held})
                    pools[pool['name']]['deployments'] = [published[d['id']] for d in pool['deployments'] if d['id'] in published]
            ordered = [pool['name'] for pool in book['config']['pools'] if pool['name'] in pools]
            ordered.extend(name for name in pools if name not in ordered)
            config['pools'] = [pools[name] for name in ordered]
            config['pool_pricing'].extend(copy.deepcopy(price) for price in book['config'].get('pool_pricing', [])
                                          if price['pool'] in empty)
            overrides = {c['user_id']: c for c in book.get('recovery_customers', [])}
            overrides.update({c['user_id']: copy.deepcopy(c) for c in (book['pending'] or {}).get('customers', [])})
            overrides.update({c['user_id']: copy.deepcopy(c) for c in remote['settings'].get('customers', [])})
            book['recovery_customers'] = list(overrides.values())
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
            view['renewals'] = copy.deepcopy(book.get('renewals', {}))
            view['account_sync'] = {name: {k: v for k, v in account['sync'].items() if k != 'basis'}
                                    for name, account in self._sync_accounts(book).items() if account['sync']}
            view['account_enabled'] = {name: account['enabled'] for name, account in self._sync_accounts(book).items()}
            view['access_token_sync'] = {}
            statuses = {a['name']: a for a in book['remote'].get('accounts', {}).get('data', [])}
            for name, account in self._sync_accounts(book).items():
                ref = account['oauth_account']
                if not ref or account['provider'] == 'workbuddy':
                    continue
                receipt = book.get('token_sync', {}).get(ref, {})
                try: digest = fingerprint(account['credential'])
                except (ValueError, TypeError, AttributeError): digest = None
                current = bool(digest and receipt.get('access_sha256') == digest
                               and statuses.get(ref, {}).get('access_sha256') == digest
                               and receipt.get('runtime_id') == book['remote']['merchant'].get('runtime_id'))
                view['access_token_sync'][name] = {'last_success_at': receipt.get('last_success_at'), 'current': current}
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
