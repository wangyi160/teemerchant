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
    daemon_threads=True
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

def main():
    parser=argparse.ArgumentParser(description='teemerchant HTTP service')
    parser.add_argument('--config',help='Path to a service JSON configuration')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8910)
    parser.add_argument('--public-url',action='append',default=[])
    base=Path(sys.executable).parent if getattr(sys,'frozen',False) else ROOT
    parser.add_argument('--data',default=str(base/'data'))
    parser.add_argument('--set-password',action='store_true')
    parser.add_argument('--tls-cert');parser.add_argument('--tls-key')
    preliminary,_=parser.parse_known_args()
    if preliminary.config:
        configuration=json.loads(Path(preliminary.config).read_text(encoding='utf-8'))
        if not isinstance(configuration,dict) or set(configuration)-{'host','port','public_urls','data','tls_cert','tls_key'}:parser.error('Invalid service configuration')
        if 'public_urls' in configuration:configuration['public_url']=configuration.pop('public_urls')
        parser.set_defaults(**configuration)
    args=parser.parse_args()
    if not 1<=args.port<=65535: parser.error('port must be 1..65535')
    if bool(args.tls_cert)!=bool(args.tls_key): parser.error('tls-cert and tls-key are required together')
    from desktop_runtime import DataLock
    from store import Store
    from app import Application
    lock=DataLock(args.data)
    password=getpass.getpass('New administrator password: ') if args.set_password else os.environ.pop('TEEMERCHANT_ADMIN_PASSWORD',None)
    auth=Auth(args.data,password)
    if args.set_password:
        print('Administrator password updated.');lock.close();return
    scheme='https' if args.tls_cert else 'http'
    origins={f'{scheme}://127.0.0.1:{args.port}',f'{scheme}://localhost:{args.port}'}
    if args.host not in ('0.0.0.0','::','127.0.0.1'): origins.add(f'{scheme}://{args.host}:{args.port}')
    for origin in args.public_url:
        u=urlsplit(origin)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment: parser.error('public-url must be an HTTP(S) origin')
        origins.add(origin.rstrip('/'))
    if args.host in ('0.0.0.0','::') and not args.public_url: parser.error('Specify --public-url for remote access')
    application=Application(Store(args.data))
    server=Server((args.host,args.port),handler(application,origins,auth))
    if args.tls_cert:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert,args.tls_key);server.socket=context.wrap_socket(server.socket,server_side=True)
    stop=threading.Event()
    worker=threading.Thread(target=application.background,args=(stop,),daemon=True);worker.start()
    def shutdown(*_):
        stop.set();threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,shutdown)
    if hasattr(signal,'SIGTERM'):signal.signal(signal.SIGTERM,shutdown)
    print(f'teemerchant listening on {args.host}:{args.port}; login: admin',flush=True)
    if auth.initial:print(f'Initial password saved to {auth.initial}',flush=True)
    try:server.serve_forever()
    finally:
        stop.set();server.server_close();worker.join(timeout=40)
        if not worker.is_alive():application.transport.close();application.store.db.close();lock.close()

if __name__=='__main__':main()
