"""Platform ledger download, independent of the attested Router control channel."""
import ipaddress
import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
from revenue import RevenueIndex, entry

INTERVAL = 300


def platform_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('管理平台地址无效')
    if not value:
        return ''
    url = urlsplit(value)
    try:
        loopback = url.hostname == 'localhost' or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = False
    try:
        port = url.port
    except ValueError:
        raise ValueError('管理平台端口无效') from None
    if (not url.hostname or url.username is not None or url.password is not None or url.path not in ('', '/')
            or url.query or url.fragment or (url.scheme != 'https' and not (url.scheme == 'http' and loopback))):
        raise ValueError('请填写管理平台 HTTPS 根地址；本机允许 HTTP')
    return value.rstrip('/')


class PlatformArchive:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.error = ''
        self.last_attempt = 0
        self.generation = 0

    def reset(self):
        self.error = ''
        self.last_attempt = 0
        self.generation += 1

    def status(self):
        with self.store.lock:
            url = self.store.settings.get('platform_url', '')
            row = self.store.db.execute('SELECT archive_id,cursor,synced_at FROM platform_cursors WHERE url=?', (url,)).fetchone()
            return {'configured': bool(url), 'error': self.error, 'interval_seconds': INTERVAL,
                    'synced_at': row[2] if row else None, 'cursor': row[1] if row else 0}

    def fetch(self, url, secret, cursor):
        # Platform receives only its issued Merchant Secret, never owner_secret,
        # OAuth tokens, account proxies, or the Router's compound control token.
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                with client.stream('GET', url+'/merchant-api/v1/records', params={'after': cursor, 'limit': 500},
                                   headers={'Authorization': 'Bearer '+secret}) as response:
                    if response.status_code != 200:
                        raise RuntimeError('管理平台账单接口返回 HTTP '+str(response.status_code))
                    chunks, size = [], 0
                    started = time.monotonic()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 16*1024*1024 or time.monotonic()-started > 25:
                            raise RuntimeError('管理平台账单响应过大或超时')
                        chunks.append(chunk)
                    return json.loads(b''.join(chunks))
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('无法读取管理平台账单，请检查平台地址和连接') from None

    def sync(self, stop=None):
        if not self.lock.acquire(blocking=False):
            return {'saved': False, 'busy': True}
        generation = self.generation
        self.last_attempt = time.monotonic()
        try:
            with self.store.lock:
                url = platform_url(self.store.settings.get('platform_url', ''))
                if not url:
                    raise ValueError('请先填写管理平台地址')
                if not self.store.credentials()['saved']:
                    raise ValueError('请先填写商户凭证')
                identity, secret = self.store.enrollment()['id'], self.store.settings['token']
                old = self.store.db.execute('SELECT archive_id,cursor FROM platform_cursors WHERE url=?', (url,)).fetchone()
                archive_id, cursor = old if old else ('', 0)
            for _ in range(16):
                if stop is not None and stop.is_set():
                    return {'saved': False, 'stopped': True}
                page = self.fetch(url, secret, cursor)
                if (not isinstance(page, dict) or page.get('version') != 1 or page.get('merchant_id') != identity
                        or not re.fullmatch(r'[a-f0-9]{32}', str(page.get('archive_id', '')))
                        or (archive_id and page['archive_id'] != archive_id)
                        or type(page.get('next_cursor')) is not int or not cursor <= page['next_cursor'] < 2**63
                        or type(page.get('has_more')) is not bool or not isinstance(page.get('data'), list)
                        or len(page['data']) > 500):
                    raise RuntimeError('管理平台账单身份或游标不一致')
                last = cursor
                for row in page['data']:
                    if (not isinstance(row, dict) or type(row.get('cursor')) is not int
                            or not last < row['cursor'] <= page['next_cursor'] or type(row.get('seq')) is not int
                            or not 0 < row['seq'] < 2**63 or not isinstance(row.get('event'), dict)
                            or row['event'].get('merchant_id') != identity):
                        raise RuntimeError('管理平台账单记录无效')
                    settlement = row['event'].get('settlement') or {}
                    if settlement.get('state') not in ('settled', 'pending'):
                        raise RuntimeError('管理平台账单结算状态无效')
                    for field in ('charge_microusd', 'official_cost_microusd'):
                        value = settlement.get(field)
                        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,40}', value)):
                            raise RuntimeError('管理平台账单金额无效')
                    if settlement['state'] == 'settled' and settlement.get('charge_microusd') is None:
                        raise RuntimeError('管理平台账单缺少结算金额')
                    last = row['cursor']
                if page['has_more'] and (not page['data'] or page['next_cursor'] <= cursor):
                    raise RuntimeError('管理平台账单分页没有前进')
                with self.store.lock, self.store.db:
                    if (generation != self.generation or self.store.enrollment()['id'] != identity
                            or self.store.settings.get('platform_url') != url):
                        raise RuntimeError('商户或管理平台已切换，本次同步已取消')
                    for row in page['data']:
                        raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                        self.store.db.execute('''INSERT INTO platform_records VALUES(?,?,?,?)
                            ON CONFLICT(url,seq) DO UPDATE SET archive_id=excluded.archive_id,payload=excluded.payload''',
                            (url, page['archive_id'], row['seq'], self.store.protect(raw)))
                    self.store.db.execute('''INSERT INTO platform_cursors VALUES(?,?,?,?)
                        ON CONFLICT(url) DO UPDATE SET archive_id=excluded.archive_id,cursor=excluded.cursor,synced_at=excluded.synced_at''',
                        (url, page['archive_id'], page['next_cursor'], int(time.time())))
                archive_id, cursor = page['archive_id'], page['next_cursor']
                if not page['has_more']:
                    self.error = ''
                    return {'saved': True}
            raise RuntimeError('账单较多，已保存当前进度，下一轮继续同步')
        except Exception as error:
            if generation == self.generation:
                self.error = str(error) if isinstance(error, (ValueError, RuntimeError)) else '本地账单保存失败，将按原游标重试'
            raise
        finally:
            self.lock.release()

    def records(self, kind):
        with self.store.lock:
            url = self.store.settings.get('platform_url', '')
            result = []
            for payload, in self.store.db.execute('SELECT payload FROM platform_records WHERE url=? ORDER BY seq DESC', (url,)):
                row = json.loads(self.store.protect(payload, True))
                if kind == 'refusals':
                    event = row['event']
                    if not event.get('refusal_type'):
                        continue
                    row = {'id': row['seq'], 'event': {k: event[k] for k in ('user_id','model','provider','created_at','refusal_type') if k in event}}
                result.append(row)
                if len(result) == 200:
                    break
            return result

    def revenue(self, query):
        index = RevenueIndex()
        with self.store.lock:
            url = self.store.settings.get('platform_url', '')
            for archive_id, seq, payload in self.store.db.execute('SELECT archive_id,seq,payload FROM platform_records WHERE url=?', (url,)):
                event = json.loads(self.store.protect(payload, True))['event']
                row = entry(archive_id, seq, event)
                settlement = event['settlement']
                known = settlement['state'] == 'settled'
                row.update(state='calculated' if known else 'pending', basis='platform_settlement',
                           reason='' if known else event.get('billing', {}).get('reason', 'pending_settlement'),
                           charge_microusd=settlement['charge_microusd'] if known else None,
                           official_cost_microusd=settlement.get('official_cost_microusd'))
                index.entries[(archive_id, seq)] = row
        return {**index.report(query), 'source': 'management_platform', 'sync': self.status()}

    def detail(self, instance, seq):
        with self.store.lock:
            row = self.store.db.execute('SELECT payload FROM platform_records WHERE url=? AND archive_id=? AND seq=?',
                                       (self.store.settings.get('platform_url', ''), instance, seq)).fetchone()
            if not row:
                raise ValueError('账单记录不存在')
            return json.loads(self.store.protect(row[0], True))
