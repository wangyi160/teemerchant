"""Authenticated Windows/Linux HTTP service. Static assets never include merchant data."""
import argparse
import getpass
import hashlib
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

class Auth:
    def __init__(self, directory, password=None):
        self.path = Path(directory) / 'http-auth.json'
        self.lock = threading.RLock()
        self.sessions, self.failures = {}, {}
        self.gate = threading.BoundedSemaphore(4)
        self.login_csrf = secrets.token_urlsafe(32)
        self.initial = None
        if password is not None or not self.path.exists():
            if password is None:
                password = secrets.token_urlsafe(24)
                self.initial = self.path.parent / 'initial-password.txt'
                fd=os.open(self.initial,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as file:
                    file.write(password+'\n');file.flush();os.fsync(file.fileno())
            if len(password)<12 or len(password)>256: raise ValueError('管理密码长度须为 12 至 256 个字符')
            salt=secrets.token_hex(16)
            record={'username':'admin','salt':salt,'hash':self.derive(password,salt).hex()}
            temporary=self.path.with_suffix('.tmp')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as file:
                json.dump(record,file);file.flush();os.fsync(file.fileno())
            os.replace(temporary,self.path)
        self.record=json.loads(self.path.read_text())

    @staticmethod
    def derive(password,salt):
        return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32)

    def login(self, body, peer):
        if not isinstance(body,dict) or not isinstance(body.get('password'),str) or len(body['password'])>256:
            raise ValueError('用户名或密码错误')
        with self.lock:
            now=time.time()
            self.failures={k:v for k,v in self.failures.items() if v[1]>now}
            if self.failures.get(peer,(0,0))[0]>=5: raise ValueError('登录失败次数过多，请五分钟后重试')
            if len(self.failures)>=4096 and peer not in self.failures: raise ValueError('登录繁忙，请稍后重试')
        if not self.gate.acquire(blocking=False): raise ValueError('登录繁忙，请稍后重试')
        try:
            valid=secrets.compare_digest(self.derive(body['password'],self.record['salt']).hex(),self.record['hash']) and body.get('username')=='admin'
        finally: self.gate.release()
        with self.lock:
            if not valid:
                self.failures[peer]=(self.failures.get(peer,(0,0))[0]+1,time.time()+300)
                raise ValueError('用户名或密码错误')
            self.failures.pop(peer,None)
            self.sessions={k:v for k,v in self.sessions.items() if v['expires']>time.time()}
            if len(self.sessions)>=64: raise ValueError('登录会话过多，请稍后重试')
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
    auth=auth or Auth(application.store.root_directory)
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
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                    **(headers or {})}.items(): self.send_header(key,value)
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
        def handle_request(self):
            origin=authorities.get(self.headers.get('Host'))
            if not origin or self.headers.get('Origin',origin)!=origin:
                return self.respond(403,{'error':'访问来源不匹配'})
            if self.headers.get('Sec-Fetch-Site')=='cross-site': return self.respond(403,{'error':'不允许跨站访问'})
            path=urlsplit(self.path).path
            session=auth.session(self.headers.get('Cookie'))
            if self.command=='GET' and path in ('/login','/login.js','/app.css'):
                name={'/login':'login.html','/login.js':'login.js','/app.css':'app.css'}[path]
                data=(ROOT/'static'/name).read_bytes().replace(b'CSRF_PLACEHOLDER',auth.login_csrf.encode())
                return self.respond(200,data,('text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css')+'; charset=utf-8')
            if path!='/api/login' and not session:
                if self.command=='GET' and path=='/': return self.respond(303,b'',headers={'Location':'/login'})
                return self.respond(401,{'error':'请先登录商户服务'})
            if self.command=='GET' and path in ('/','/app.js'):
                if path=='/':
                    data=(ROOT/'static/index.html').read_bytes().replace(b'CSRF_PLACEHOLDER',session['csrf'].encode())
                else:
                    data=b'\n'.join((ROOT/'static'/name).read_bytes() for name in ['web-bridge.js','account-config.js','app.js','account-ui.js','revenue-ui.js','oauth-ui.js'])
                return self.respond(200,data,('text/html' if path=='/' else 'text/javascript')+'; charset=utf-8')
            expected=auth.login_csrf if path=='/api/login' else session['csrf']
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),expected): return self.respond(403,{'error':'页面会话无效，请刷新'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>16*1024*1024 or self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))>1:
                    return self.respond(413,{'error':'请求过大或格式无效'})
                if length and self.headers.get_content_type()!='application/json': return self.respond(415,{'error':'需要 JSON 请求'})
                body=json.loads(self.rfile.read(length)) if length else {}
                if self.command=='POST' and path=='/api/login':
                    token=auth.login(body,self.client_address[0])
                    cookie=f'tm_session={token}; HttpOnly; SameSite=Strict; Path=/' + ('; Secure' if origin.startswith('https:') else '')
                    return self.respond(200,{'logged_in':True},headers={'Set-Cookie':cookie})
                if self.command=='POST' and path=='/api/logout':
                    with auth.lock: auth.sessions.pop(session['id'],None)
                    with application.state_lock:
                        application.oauth.flows={k:f for k,f in application.oauth.flows.items() if f['session']!=session['id']}
                    return self.respond(200,{'logged_out':True},headers={'Set-Cookie':'tm_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/'})
                if path.startswith('/api/oauth/') and self.command=='POST':
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
    parser.add_argument('--set-password',action='store_true')
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
    if not isinstance(args.public_url,list) or not all(isinstance(o,str) for o in args.public_url):parser.error('public_urls must be an array of origins')
    if not isinstance(args.data,str) or not args.data:parser.error('Invalid data directory')
    if any(value is not None and not isinstance(value,str) for value in (args.tls_cert,args.tls_key)):parser.error('Invalid TLS file path')
    if bool(args.tls_cert)!=bool(args.tls_key): parser.error('tls-cert and tls-key are required together')
    args.data=str(Path(args.data).resolve())
    scheme='https' if args.tls_cert else 'http'
    origins={f'{scheme}://127.0.0.1:{args.port}',f'{scheme}://localhost:{args.port}'}
    host=f'[{args.host}]' if ':' in args.host else args.host
    if args.host not in ('0.0.0.0','::','127.0.0.1'): origins.add(f'{scheme}://{host}:{args.port}')
    for origin in args.public_url:
        u=urlsplit(origin)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment: parser.error('public-url must be an HTTP(S) origin')
        origins.add(origin.rstrip('/'))
    if args.host in ('0.0.0.0','::') and not args.public_url: parser.error('Specify --public-url for remote access')
    args.origins=origins
    local_host='127.0.0.1' if args.host=='0.0.0.0' else '[::1]' if args.host=='::' else host
    args.url=args.public_url[0].rstrip('/') if args.public_url else f'{scheme}://{local_host}:{args.port}'
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
        lock=store=application=server=http_worker=worker=None
        try:
            lock=DataLock(args.data)
            auth=Auth(args.data,os.environ.pop('TEEMERCHANT_ADMIN_PASSWORD',None))
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
            http_worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.2},name='merchant-http',daemon=True)
            http_worker.start()
            self.started_at=time.monotonic()
            self.state='stopping' if self.stop.is_set() else 'running'
            print(f'teemerchant listening on {args.host}:{args.port}; login: admin',flush=True)
            if auth.initial:print(f'Initial password saved to {auth.initial}',flush=True)
            while not self.stop.wait(.25):
                if not http_worker.is_alive() or not worker.is_alive():
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
    if args.set_password:
        from desktop_runtime import DataLock
        lock=DataLock(args.data)
        try:Auth(args.data,getpass.getpass('New administrator password: '))
        finally:lock.close()
        print('Administrator password updated.')
        return 0
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
