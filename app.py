"""Local merchant console; the merchant owns its identity, TEE policy and archives."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import webbrowser
from store import Store
from transport import Transport

ROOT = Path(__file__).parent

class Application:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport or Transport(store)
        self.csrf = secrets.token_urlsafe(32)
        self.connected = False
        self.remote = None
        self.last_error = ''
        self.sync_lock = threading.Lock()
        self.state_lock = threading.RLock()

    def accept(self, result):
        if result['merchant']['id'] != self.store.enrollment()['id']:
            raise RuntimeError('商户身份与本机身份不一致')
        revision = result['settings']['revision']
        if revision < self.store.settings['minimum_revision']:
            raise RuntimeError('Router 状态早于本机已确认版本，已拒绝回退')
        self.store.settings['minimum_revision'] = revision
        self.store.save()
        self.remote, self.connected = result, True
        return result

    def sync(self):
        with self.state_lock, self.sync_lock:
            for kind, path, field in [('usage', '/merchant/usage', 'seq'), ('refusals', '/merchant/refusals', 'id')]:
                for _ in range(16):
                    result = self.transport.call('GET', path + '?limit=500')
                    rows = result['data']
                    if not rows:
                        break
                    self.store.archive(kind, result['instance_id'], rows)
                    through = rows[-1][field]
                    if kind == 'usage':
                        self.transport.call('POST', path + '/ack', {'instance_id': result['instance_id'], 'through_seq': through})
                    else:
                        self.transport.call('DELETE', path + f'?through_id={through}')
            self.last_error = ''
        return {'saved': True}

    def api(self, method, path, body):
        # Status remains responsive while a remote archive operation is waiting.
        if (method, path) == ('GET', '/api/state'):
            with self.store.lock:
                return self._api(method, path, body)
        with self.state_lock:
            return self._api(method, path, body)

    def _api(self, method, path, body):
        if (method, path) == ('GET', '/api/state'):
            return {'settings': self.store.public(), 'enrollment': self.store.enrollment(), 'connected': self.connected, 'remote': self.remote, 'error': self.last_error}
        if (method, path) == ('PUT', '/api/settings'):
            if set(body) != {'router', 'tdx_policy', 'snp_policy', 'transport'} or any(not isinstance(v, str) or len(v) > 4096 for v in body.values()):
                raise ValueError('连接设置无效')
            self.transport.close()
            self.store.settings.update(body)
            self.store.save()
            self.remote, self.connected = None, False
            return {'saved': True}
        if (method, path) == ('POST', '/api/verify'):
            return self.transport.call('VERIFY', '')
        if (method, path) == ('POST', '/api/connect'):
            return self.accept(self.transport.call('POST', '/merchant/unlock', {'minimum_revision': self.store.settings['minimum_revision']}))
        if (method, path) == ('GET', '/api/merchant'):
            return self.accept(self.transport.call('GET', '/merchant/state'))
        if (method, path) == ('PUT', '/api/config'):
            return self.accept(self.transport.call('PUT', '/merchant/config', body))
        if (method, path) == ('POST', '/api/refresh'):
            return self.accept(self.transport.call('POST', '/merchant/models/refresh'))
        if (method, path) == ('POST', '/api/sync'):
            return self.sync()
        if method == 'GET' and path in ('/api/records/usage', '/api/records/refusals'):
            return {'data': self.store.records(path.rsplit('/', 1)[1])}
        raise ValueError('接口不存在')

    def background(self, stop):
        while not stop.wait(10):
            if self.connected:
                try:
                    self.sync()
                except Exception as error:
                    self.last_error = str(error)

def handler(application, origin):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            pass

        def respond(self, status, value, mime='application/json; charset=utf-8'):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            if self.headers.get('Host') != urlsplit(origin).netloc or self.headers.get('Origin', origin) != origin:
                return self.respond(403, {'error': '仅允许本机同源访问'})
            path = urlsplit(self.path).path
            if self.command == 'GET' and path in ('/', '/app.js', '/app.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/app.css': 'app.css'}[path]
                data = (ROOT / 'static' / name).read_bytes()
                if name == 'index.html':
                    data = data.replace(b'CSRF_PLACEHOLDER', application.csrf.encode())
                mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'app.css': 'text/css'}[name] + '; charset=utf-8'
                return self.respond(200, data, mime)
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), application.csrf):
                return self.respond(403, {'error': '页面会话无效，请刷新'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 16 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    return self.respond(413, {'error': '请求过大'})
                body = json.loads(self.rfile.read(length)) if length else {}
                return self.respond(200, application.api(self.command, path, body))
            except (ValueError, RuntimeError) as error:
                return self.respond(400, {'error': str(error)})
            except Exception:
                return self.respond(503, {'error': '本地管理操作失败，请检查配置与验证组件'})

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_DELETE = handle_request
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8910)
    parser.add_argument('--data', default=str(Path(os.environ.get('LOCALAPPDATA', '.')) / 'teemerchant'))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    application = Application(Store(args.data))
    origin = f'http://127.0.0.1:{args.port}'
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(application, origin))
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=application.background, args=(stop,), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(origin)
    print(origin, flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        application.transport.close()
        server.server_close()

if __name__ == '__main__':
    main()
