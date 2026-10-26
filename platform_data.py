"""On-demand platform queries. No ledger mirror, cursor or background downloader."""
import json
import time
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
from platform_archive import platform_url


class PlatformData:
    def __init__(self, store):
        self.store = store
        self.generation = 0

    def reset(self):
        self.generation += 1

    def context(self):
        with self.store.lock:
            return (self.generation, self.store.enrollment()['id'],
                    platform_url(self.store.settings.get('platform_url', '')), self.store.settings['token'])

    def fetch(self, path):
        return self.request('GET', path)

    def request(self, method, path, payload=None, operation=None):
        context = self.context()
        if not context[2]:
            raise ValueError('请先填写管理平台地址')
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=90 if method != 'GET' else 15) as client:
                headers = {'Authorization': 'Bearer ' + context[3]}
                if operation:
                    headers['Idempotency-Key'] = operation
                with client.stream(method, context[2] + '/merchant-api/v1/' + path,
                                   headers=headers, **({'json': payload} if payload is not None else {})) as response:
                    if response.status_code != 200:
                        raise RuntimeError('管理平台查询失败：HTTP ' + str(response.status_code))
                    chunks, size, started = [], 0, time.monotonic()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 16 * 1024 * 1024 or time.monotonic() - started > 25:
                            raise RuntimeError('管理平台响应过大或超时')
                        chunks.append(chunk)
                    value = json.loads(b''.join(chunks))
                    if not isinstance(value, dict):
                        raise ValueError()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('暂时无法读取管理平台数据，将自动重试') from None
        if context != self.context():
            raise ValueError('商户或管理平台已切换，本次查询已取消')
        return value

    def configure(self, body):
        operation = secrets.token_hex(16)
        try:
            reply = self.request('PUT', 'config', body, operation)
        except RuntimeError as error:
            try:
                reply = self.fetch('commands/' + operation)
            except Exception:
                raise RuntimeError('管理平台未确认配置结果，保留上次确认值；请刷新后重试') from error
        if reply.get('status') != 'applied' or reply.get('operation_id') != operation:
            raise RuntimeError('配置未获 Router 确认，保留上次确认值：' + str(reply.get('error', reply.get('status', 'invalid_response'))))
        return reply['state']

    def read(self, path):
        url = urlsplit(path)
        if url.path == '/api/revenue':
            return self.fetch('revenue' + ('?' + url.query if url.query else ''))
        if url.path == '/api/revenue/record':
            values = parse_qs(url.query, keep_blank_values=True)
            if set(values) != {'instance', 'seq'} or any(len(v) != 1 for v in values.values()) or not values['seq'][0].isdigit():
                raise ValueError('账单记录标识无效')
            return self.fetch('activity/' + values['seq'][0])
        kind = url.path.rsplit('/', 1)[1]
        if kind not in ('usage', 'refusals'):
            raise ValueError('记录类型无效')
        values = parse_qs(url.query, keep_blank_values=True)
        if set(values) - {'before'} or any(len(v) != 1 or not v[0].isdigit() for v in values.values()):
            raise ValueError('分页参数无效')
        return self.fetch('activity?kind=' + kind + '&limit=50' + ('&before=' + values['before'][0] if values else ''))
