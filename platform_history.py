"""Merchant-owned withdrawal history and signed receipt copies from the platform."""
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from platform_archive import INTERVAL, platform_url


class PlatformHistory:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.generation = 0
        self.last_attempt = 0
        self.error = ''

    def reset(self):
        self.generation += 1
        self.last_attempt = 0
        self.error = ''

    def context(self):
        with self.store.lock:
            return (self.generation, self.store.enrollment()['id'],
                    platform_url(self.store.settings.get('platform_url', '')), self.store.settings['token'])

    def check(self, context):
        if context != self.context():
            raise RuntimeError('商户或管理平台已切换，本次同步已取消')

    def get(self, context, kind, key):
        with self.store.lock:
            self.check(context)
            row = self.store.db.execute('SELECT payload FROM platform_history WHERE url=? AND kind=? AND key=?',
                                       (context[2], kind, str(key))).fetchone()
            return json.loads(self.store.protect(row[0], True)) if row else None

    def put(self, context, kind, key, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        with self.store.lock, self.store.db:
            self.check(context)
            self.store.db.execute('INSERT INTO platform_history VALUES(?,?,?,?) '
                                  'ON CONFLICT(url,kind,key) DO UPDATE SET payload=excluded.payload',
                                  (context[2], kind, str(key), self.store.protect(raw)))

    def exists(self, context, kind, key):
        with self.store.lock:
            self.check(context)
            return self.store.db.execute('SELECT 1 FROM platform_history WHERE url=? AND kind=? AND key=?',
                                         (context[2], kind, str(key))).fetchone() is not None

    def status(self):
        with self.store.lock:
            context = self.context()
            meta = self.get(context, 'meta', 'status') or {}
            return {**meta, 'error': self.error, 'syncing': self.lock.locked(), 'interval_seconds': INTERVAL}

    def fetch(self, context, path):
        self.check(context)
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                with client.stream('GET', context[2] + '/merchant-api/v1/' + path,
                                   headers={'Authorization': 'Bearer ' + context[3]}) as response:
                    if response.status_code != 200:
                        raise RuntimeError('管理平台记录接口返回 HTTP ' + str(response.status_code))
                    chunks, size, started = [], 0, time.monotonic()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 16 * 1024 * 1024 or time.monotonic() - started > 25:
                            raise RuntimeError('管理平台记录响应过大或超时')
                        chunks.append(chunk)
                    value = json.loads(b''.join(chunks))
                    if not isinstance(value, dict):
                        raise ValueError()
                    return value
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('无法读取管理平台记录，将自动重试；本地记录已保留') from None

    @staticmethod
    def number(value):
        if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,18}', str(value)) or int(value) >= 2**63:
            raise RuntimeError('管理平台记录编号无效')
        return int(value)

    def save_withdrawal(self, context, row):
        key = self.number(row.get('id'))
        if row.get('state') not in ('pending', 'processing', 'paid', 'rejected', 'cancelled'):
            raise RuntimeError('管理平台提现状态无效')
        with self.store.lock:
            old = self.get(context, 'withdrawals', key)
            # A concurrent create/cancel response must not be overwritten by an older GET.
            if old and old.get('version', 0) > row.get('version', 0):
                return
            if old and old.get('version') == row.get('version') and 'events' not in row:
                row = {**old, **row}
            self.put(context, 'withdrawals', key, row)

    def save_receipt(self, context, row):
        seq = self.number(row.get('seq'))
        digest = row.get('batch_hash')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise RuntimeError('管理平台凭据摘要无效')
        shared = 'batch:' + digest
        proof_key = shared if self.exists(context, 'proofs', shared) else 'seq:' + str(seq)
        if not self.exists(context, 'proofs', proof_key):
            proof = self.fetch(context, 'receipts/records/' + str(seq))
            if proof.get('batch', {}).get('batch_hash') != digest:
                raise RuntimeError('管理平台凭据与记录不匹配')
            manifest = json.loads(proof['batch']['manifest'])
            if manifest.get('schema') == 'teerouter.public-consumption-batch.v1':
                proof_key = shared
                proof.pop('selected_sequence', None)
            self.put(context, 'proofs', proof_key, proof)
        self.put(context, 'receipts', seq, {**row, 'proof_key': proof_key})

    def scan(self, context, kind, stop):
        """Incremental newest-first scan, with a durable bookmark for large histories."""
        scan = self.get(context, 'scan', kind) or {'head': 0, 'before': None, 'new_head': 0}
        path = 'withdrawals' if kind == 'withdrawals' else 'receipts/mine'
        field = 'id' if kind == 'withdrawals' else 'seq'
        deadline = time.monotonic() + 60
        for _ in range(8):
            if stop is not None and stop.is_set():
                return False
            before = scan['before']
            page = self.fetch(context, path + ('?before=' + str(before) if before else ''))
            rows = page.get('data')
            if not isinstance(rows, list) or len(rows) > 50 or type(page.get('has_more')) is not bool:
                raise RuntimeError('管理平台记录分页无效')
            last = before or 2**63
            reached = False
            for row in rows:
                if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                    return False
                key = self.number(row.get(field))
                if key >= last:
                    raise RuntimeError('管理平台记录分页顺序无效')
                last = key
                scan['new_head'] = max(scan['new_head'], key)
                if kind == 'withdrawals':
                    old = self.get(context, kind, key)
                    if not old or old.get('version') != row.get('version') or 'events' not in old:
                        detail = self.fetch(context, 'withdrawals/' + str(key))
                        if self.number(detail.get('id')) != key:
                            raise RuntimeError('管理平台提现记录不匹配')
                        self.save_withdrawal(context, detail)
                else:
                    self.save_receipt(context, row)
                if key <= scan['head']:
                    reached = True
                    break
                scan['before'] = key
                self.put(context, 'scan', kind, scan)
            if kind == 'withdrawals':
                if not isinstance(page.get('balance'), dict) or not isinstance(page.get('fee_policy'), dict):
                    raise RuntimeError('管理平台提现余额或费率无效')
                self.put(context, 'meta', 'withdrawals', {k: v for k, v in page.items()
                         if k not in ('data', 'has_more', 'next_before')})
            if reached or not page['has_more']:
                self.put(context, 'scan', kind, {'head': max(scan['head'], scan['new_head']), 'before': None, 'new_head': 0})
                return True
            if not rows or self.number(page.get('next_before')) != last:
                raise RuntimeError('管理平台记录分页没有前进')
            scan['before'] = last
            self.put(context, 'scan', kind, scan)
        return False

    def rows(self, context, kind, before=2**63-1, limit=None):
        with self.store.lock:
            self.check(context)
            return [json.loads(self.store.protect(row[0], True)) for row in self.store.db.execute(
                'SELECT payload FROM platform_history WHERE url=? AND kind=? AND CAST(key AS INTEGER)<? '
                'ORDER BY CAST(key AS INTEGER) DESC LIMIT ?', (context[2], kind, before, limit if limit is not None else -1))]

    def sync(self, stop=None):
        if not self.lock.acquire(blocking=False):
            return {'busy': True}
        context = self.context()
        self.last_attempt = time.monotonic()
        errors, complete = [], True
        try:
            for kind in ('withdrawals', 'receipts'):
                try:
                    complete = self.scan(context, kind, stop) and complete
                    if kind == 'withdrawals':
                        # Old pending applications can change even after leaving the newest page.
                        for row in self.rows(context, kind):
                            if stop is not None and stop.is_set():
                                return {'stopped': True}
                            if row['state'] in ('pending', 'processing'):
                                detail = self.fetch(context, 'withdrawals/' + str(row['id']))
                                if str(detail.get('id')) != str(row['id']):
                                    raise RuntimeError('管理平台提现记录不匹配')
                                self.save_withdrawal(context, detail)
                except Exception as error:
                    errors.append(str(error) if isinstance(error, (RuntimeError, ValueError)) else '本地记录保存失败')
            with self.store.lock:
                self.check(context)
                self.error = '；'.join(errors)
                if not errors:
                    self.put(context, 'meta', 'status', {'synced_at': int(time.time()), 'complete': complete})
            return {'saved': not errors, 'complete': complete}
        finally:
            self.lock.release()

    def read(self, path):
        with self.store.lock:
            context = self.context()
            url = urlsplit(path)
            values = parse_qs(url.query, keep_blank_values=True)
            if set(values) - {'before'} or any(len(v) != 1 for v in values.values()):
                raise ValueError('分页参数无效')
            before = self.number(values['before'][0]) if values.get('before') else 2**63-1
            if url.path in ('withdrawals', 'receipts/mine'):
                kind = 'withdrawals' if url.path == 'withdrawals' else 'receipts'
                field = 'id' if kind == 'withdrawals' else 'seq'
                data = self.rows(context, kind, before, 51)
                meta = (self.get(context, 'meta', kind) or {}) if kind == 'withdrawals' else {}
                if kind == 'withdrawals' and not meta:
                    raise RuntimeError('提现记录正在自动同步')
                return {**meta, 'data': [{k: v for k, v in r.items() if k != 'proof_key'} for r in data[:50]],
                        'has_more': len(data) > 50, 'next_before': data[49][field] if len(data) > 50 else None,
                        'local_sync': self.status()}
            key = self.number(url.path.rsplit('/', 1)[1])
            if url.path.startswith('withdrawals/'):
                row = self.get(context, 'withdrawals', key)
                if not row:
                    raise ValueError('本地提现记录不存在，正在自动同步')
                return row
            row = self.get(context, 'receipts', key)
            proof = self.get(context, 'proofs', row['proof_key']) if row else None
            if not proof:
                raise ValueError('本地凭据不存在，正在自动同步')
            if row['proof_key'].startswith('batch:'):
                proof['selected_sequence'] = key
            return proof
