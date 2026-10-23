"""Loopback-only Windows/Linux merchant UI with automatic local browser sessions."""
import argparse
import ipaddress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import ssl
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).parent

class LocalSessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}

    def create(self):
        with self.lock:
            self.sessions={k:v for k,v in self.sessions.items() if v['expires']>time.time()}
            if len(self.sessions)>=64:self.sessions.pop(next(iter(self.sessions)))
            token=secrets.token_urlsafe(32)
            self.sessions[token]={'id':token,'csrf':secrets.token_urlsafe(32),'expires':time.time()+12*3600}
            return token

    def session(self, header):
        try:
            cookies=SimpleCookie();cookies.load(header or '')
            token=cookies['tm_session'].value
        except (KeyError,ValueError): return None
        with self.lock:
            session=self.sessions.get(token)
            return session.copy() if session and session['expires']>time.time() else None

def handler(application, origins, auth=None):
    if isinstance(origins,str): origins={origins}
    origins={o.rstrip('/') for o in origins}
    auth=auth or LocalSessions()
    if any(urlsplit(o).hostname not in ('127.0.0.1','::1','localhost') for o in origins):
        raise ValueError('商户管理页面只允许本机访问')
    authorities={urlsplit(o).netloc:o for o in origins}
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup();self.connection.settimeout(15)
        def log_message(self,*_): pass
        def respond(self,status,value,mime='application/json; charset=utf-8',headers=None):
            data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            for key,value in {'Content-Type':mime,'Content-Length':str(len(data)),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'",
                    **(headers or {})}.items(): self.send_header(key,value)
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass
        def handle_request(self):
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return self.respond(403,{'error':'商户管理页面只允许本机访问'})
            if len(self.headers.get_all('Host',[]))!=1 or len(self.headers.get_all('Origin',[]))>1:
                return self.respond(403,{'error':'访问来源不匹配'})
            origin=authorities.get(self.headers.get('Host'))
            if not origin or self.headers.get('Origin',origin)!=origin:
                return self.respond(403,{'error':'访问来源不匹配'})
            if self.headers.get('Sec-Fetch-Site') not in (None,'none','same-origin'):
                return self.respond(403,{'error':'不允许跨站访问'})
            path=urlsplit(self.path).path
            session=auth.session(self.headers.get('Cookie'))
            if self.command=='GET' and path=='/login':return self.respond(303,b'',headers={'Location':'/'})
            if self.command=='GET' and path in ('/account','/account/login','/account/register'):
                suffix='' if path=='/account' else '/'+path.rsplit('/',1)[1]
                return self.respond(303,b'',headers={'Location':'/#connection'+suffix})
            if self.command=='GET' and path=='/':
                headers={}
                if not session:
                    token=auth.create()
                    cookie=f'tm_session={token}; HttpOnly; SameSite=Strict; Path=/' + ('; Secure' if origin.startswith('https:') else '')
                    headers['Set-Cookie']=cookie
                    session=auth.session(cookie)
                data=(ROOT/'static/index.html').read_bytes().replace(b'CSRF_PLACEHOLDER',session['csrf'].encode())
                return self.respond(200,data,'text/html; charset=utf-8',headers)
            if not session:return self.respond(401,{'error':'请打开本机管理页面'})
            if self.command=='GET' and path in ('/account/finance','/account/receipts'):
                name=path.rsplit('/',1)[1]
                file=name+'.html' if name in ('login','register') else 'index.html'
                data=(ROOT/'static/platform'/file).read_bytes().replace(b'LOCAL_CSRF',session['csrf'].encode()).replace(b'MERCHANT_ID',application.store.enrollment()['id'].encode())
                mode='merchant' if name in ('finance','receipts') else 'account'
                data=data.replace(b'ACCOUNT_MODE',mode.encode())
                return self.respond(200,data,'text/html; charset=utf-8')
            if self.command=='GET' and path.startswith('/account-assets/'):
                name=path[len('/account-assets/'):]
                allowed={'common.css','local.css','browser-utils.js','common.js','withdrawals.js','finance.js','workspace-receipts.js','merchant.js','workspace.js','local.js','auth.js'}
                if name not in allowed:return self.respond(404,{'error':'not_found'})
                return self.respond(200,(ROOT/'static/platform'/name).read_bytes(),'text/css; charset=utf-8' if name.endswith('.css') else 'text/javascript; charset=utf-8')
            if self.command=='GET' and path=='/app.css':
                return self.respond(200,(ROOT/'static/app.css').read_bytes(),'text/css; charset=utf-8')
            if self.command=='GET' and path=='/app.js':
                data=b'\n'.join((ROOT/'static'/name).read_bytes() for name in ['web-bridge.js','account-config.js','app.js','connection-ui.js','account-ui.js','revenue-ui.js','oauth-ui.js'])
                return self.respond(200,data,'text/javascript; charset=utf-8')
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']): return self.respond(403,{'error':'页面会话无效，请刷新'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>16*1024*1024 or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))>1:
                    return self.respond(413,{'error':'请求过大或格式无效'})
                if length and self.headers.get_content_type()!='application/json': return self.respond(415,{'error':'需要 JSON 请求'})
                body=json.loads(self.rfile.read(length)) if length else {}
                if path.startswith('/account-api/'):
                    result=application.platform_client.call(session['id'],self.headers.get('X-Account-Mode','account'),self.command,
                        self.path[len('/account-api/'):],body,self.headers.get('Idempotency-Key',''),self.headers.get('X-Merchant-ID',''))
                    return self.respond(result.status,result.data,result.mime)
                elif path.startswith('/api/oauth/') and self.command=='POST':
                    with application.state_lock:
                        result=application.oauth.dispatch(path.rsplit('/',1)[1],body,self.headers.get('X-Merchant-ID'),session['id'])
                else:
                    result=application.api(self.command,self.path,body,self.headers.get('X-Merchant-ID'))
                return self.respond(200,result)
            except (ValueError,RuntimeError) as error: return self.respond(400,{'error':str(error)})
            except Exception: return self.respond(503,{'error':'商户服务操作失败，请检查配置和账号状态'})
        do_GET=handle_request
        do_POST=handle_request
        do_PUT=handle_request
        do_DELETE=handle_request
    return Handler

class Server(ThreadingHTTPServer):
    # Drain active handlers before closing the vault or releasing its lock.
    daemon_threads=False
    allow_reuse_address=os.name!='nt'
    def server_bind(self):
        if self.server_address[0] not in ('127.0.0.1','::1','localhost'):
            raise ValueError('商户服务只能监听本机回环地址')
        if os.name=='nt':self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(32)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False): request.close();return
        try: super().process_request(request,address)
        except Exception: self.slots.release();raise
    def process_request_thread(self,*args):
        try: super().process_request_thread(*args)
        finally: self.slots.release()

class ConfigurationParser(argparse.ArgumentParser):
    def error(self,message):raise ValueError(message)


def options(argv=None, base=None):
    base=Path(base) if base is not None else (Path(sys.executable).parent if getattr(sys,'frozen',False) else ROOT)
    parser=ConfigurationParser(description='teemerchant HTTP service')
    parser.add_argument('--config',default=str(base/'service.json') if (base/'service.json').is_file() else None,help='Path to a service JSON configuration')
    parser.add_argument('--headless',action='store_true',help='Run without the Windows status window and tray')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8910)
    parser.add_argument('--public-url',action='append',default=[])
    parser.add_argument('--data',default=str(base/'data'))
    parser.add_argument('--tls-cert');parser.add_argument('--tls-key')
    preliminary,_=parser.parse_known_args(argv)
    if preliminary.config:
        configuration=json.loads(Path(preliminary.config).read_text(encoding='utf-8'))
        if not isinstance(configuration,dict) or set(configuration)-{'host','port','public_urls','data','tls_cert','tls_key'}:parser.error('Invalid service configuration')
        for key in ('data','tls_cert','tls_key'):
            if configuration.get(key):configuration[key]=str((Path(preliminary.config).resolve().parent/str(configuration[key])).resolve())
        if 'public_urls' in configuration:configuration['public_url']=configuration.pop('public_urls')
        if preliminary.public_url:configuration.pop('public_url',None)
        parser.set_defaults(**configuration)
    args=parser.parse_args(argv)
    if type(args.port) is not int or not 1<=args.port<=65535: parser.error('port must be 1..65535')
    if not isinstance(args.host,str) or not args.host or any(c in args.host for c in '/\\?#@'):parser.error('Invalid host')
    if args.host not in ('127.0.0.1','::1','localhost'):parser.error('商户服务只能监听 127.0.0.1 或 ::1，不支持局域网访问')
    if args.host=='localhost':args.host='127.0.0.1'
    if not isinstance(args.public_url,list) or not all(isinstance(o,str) for o in args.public_url):parser.error('public_urls must be an array of origins')
    if not isinstance(args.data,str) or not args.data:parser.error('Invalid data directory')
    if any(value is not None and not isinstance(value,str) for value in (args.tls_cert,args.tls_key)):parser.error('Invalid TLS file path')
    if bool(args.tls_cert)!=bool(args.tls_key): parser.error('tls-cert and tls-key are required together')
    args.data=str(Path(args.data).resolve())
    scheme='https' if args.tls_cert else 'http'
    origins={f'{scheme}://127.0.0.1:{args.port}',f'{scheme}://localhost:{args.port}'}
    host=f'[{args.host}]' if ':' in args.host else args.host
    if args.host=='::1':origins.add(f'{scheme}://[::1]:{args.port}')
    for origin in args.public_url:
        u=urlsplit(origin)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment: parser.error('public-url must be an HTTP(S) origin')
        if u.hostname not in ('127.0.0.1','::1','localhost') or u.port!=args.port or u.scheme!=scheme:
            parser.error('商户管理地址必须是本机回环地址，并与监听端口和协议一致')
        origins.add(origin.rstrip('/'))
    args.origins=origins
    args.url=f'{scheme}://{host}:{args.port}'
    return args


class Runtime:
    """One service lifetime, owned by the CLI or the native Windows message loop."""
    def __init__(self,args):
        self.args=args
        self.stop=threading.Event()
        self.done=threading.Event()
        self.state='starting'
        self.error=''
        self.started_at=None
        self.thread=None

    def start(self):
        if self.thread is not None:raise RuntimeError('Service already started')
        self.thread=threading.Thread(target=self.run,name='merchant-service',daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if not self.done.is_set():self.state='stopping'

    def run(self):
        from desktop_runtime import DataLock
        from store import Store
        from app import Application
        args=self.args
        lock=store=application=server=http_worker=worker=archive_worker=None
        try:
            lock=DataLock(args.data)
            auth=LocalSessions()
            store=Store(args.data)
            application=Application(store)
            server_type=type('IPv6Server',(Server,),{'address_family':socket.AF_INET6}) if ':' in args.host else Server
            server=server_type((args.host,args.port),handler(application,args.origins,auth))
            if args.tls_cert:
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(args.tls_cert,args.tls_key)
                server.socket=context.wrap_socket(server.socket,server_side=True)
            worker=threading.Thread(target=application.background,args=(self.stop,),name='merchant-renewal',daemon=True)
            worker.start()
            archive_worker=threading.Thread(target=application.archive_background,args=(self.stop,),name='merchant-platform-archive',daemon=True)
            archive_worker.start()
            http_worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.2},name='merchant-http',daemon=True)
            http_worker.start()
            self.started_at=time.monotonic()
            self.state='stopping' if self.stop.is_set() else 'running'
            print(f'teemerchant local management: {args.url}',flush=True)
            while not self.stop.wait(.25):
                if not http_worker.is_alive() or not worker.is_alive() or not archive_worker.is_alive():
                    raise RuntimeError('HTTP 服务或后台任务已停止')
        except OSError as error:
            code=getattr(error,'winerror',None) or error.errno
            self.error=(f'端口 {args.port} 已被占用或禁止监听' if code in (48,98,10048,10013) else '无法启动服务，请检查监听地址、目录权限和 TLS 配置')
        except (ValueError,RuntimeError) as error:
            # Local setup errors only; never expose remote credential responses.
            self.error=str(error)
        except Exception:
            self.error='服务启动失败，请检查配置和完整程序文件'
        finally:
            self.stop.set()
            self.state='stopping'
            try:
                if server:
                    if http_worker and http_worker.is_alive():server.shutdown();http_worker.join()
                    server.server_close()
                if worker:worker.join()
                if archive_worker:archive_worker.join()
                if application:application.transport.close()
                if application:application.store.db.close()
                elif store:store.db.close()
            finally:
                if lock:lock.close()
                self.state='failed' if self.error else 'stopped'
                self.done.set()


def main():
    try:args=options()
    except (ValueError,OSError) as error:
        message='服务配置无效：'+str(error)
        print(message,file=sys.stderr)
        if os.name=='nt' and '--headless' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'teemerchant',0x10)
        return 2
    runtime=Runtime(args)
    if os.name=='nt' and not args.headless:
        from windows_status import run
        return run(runtime)
    signal.signal(signal.SIGINT,lambda *_:runtime.shutdown())
    if hasattr(signal,'SIGTERM'):signal.signal(signal.SIGTERM,lambda *_:runtime.shutdown())
    if hasattr(signal,'SIGBREAK'):signal.signal(signal.SIGBREAK,lambda *_:runtime.shutdown())
    runtime.start()
    runtime.done.wait()
    if runtime.error:print(runtime.error,file=sys.stderr)
    return 1 if runtime.error else 0

if __name__=='__main__':sys.exit(main())
