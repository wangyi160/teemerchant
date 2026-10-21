"""Official OAuth HTTP over the account's configured proxy, never a direct fallback."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from urllib.parse import urlsplit, parse_qsl, unquote
import uuid
import httpx

HOSTS = {'openai': {'auth.openai.com'}, 'anthropic': {'claude.ai', 'api.anthropic.com', 'platform.claude.com'}, 'xai': {'auth.x.ai'}}

class OAuthError(RuntimeError):
    def __init__(self, message, *, uncertain=False, permanent=False):
        super().__init__(message)
        self.uncertain, self.permanent = uncertain, permanent

def official(provider, url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in HOSTS.get(provider, ()) or u.port or u.username or u.password or u.query or u.fragment:
        raise ValueError('OAuth 服务地址不是预期的官方地址')
    return url

def vless_config(value):
    try:
        u = urlsplit(value)
        pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
        q = dict(pairs)
        if (len(pairs) != len(q) or set(q) - {'security','encryption','type','sni','host','path','mode','fp','alpn','flow'}
                or u.scheme != 'vless' or not u.hostname or not u.port or u.password or u.path not in ('', '/')
                or str(uuid.UUID(u.username)) != u.username.lower() or len(value) > 8192
                or any(ord(c) < 32 for c in value)):
            raise ValueError()
        network = q.get('type', 'tcp')
        if q.get('security') != 'tls' or q.get('encryption', 'none') != 'none' or network not in ('tcp', 'xhttp'):
            raise ValueError()
        mode = q.get('mode', 'auto'); flow = q.get('flow', '')
        if mode not in ('auto','stream-one','stream-up','packet-up') or flow not in ('','xtls-rprx-vision') or (network == 'xhttp' and flow):
            raise ValueError()
        sni, host = q.get('sni', u.hostname), q.get('host', '')
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', sni) or (host and not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}', host)):
            raise ValueError()
        path = q.get('path', '/')
        if not path.startswith('/') or len(path) > 2048 or any(ord(c) < 32 for c in path):
            raise ValueError()
        tls = {'serverName': sni, 'allowInsecure': False}
        if q.get('fp'):
            if q['fp'] not in ('chrome','firefox','safari','ios','android','edge','360','qq','random','randomized'):
                raise ValueError()
            tls['fingerprint'] = q['fp']
        if q.get('alpn'):
            tls['alpn'] = q['alpn'].split(',')
            if any(a not in ('h2','http/1.1') for a in tls['alpn']): raise ValueError()
        stream = {'network': network, 'security': 'tls', 'tlsSettings': tls}
        if network == 'xhttp': stream['xhttpSettings'] = {'host': host, 'path': path, 'mode': mode}
        return {'log': {'access':'none','error':'none','loglevel':'none'}, 'outbounds': [{'tag':'node','protocol':'vless',
            'settings': {'vnext':[{'address':u.hostname,'port':u.port,'users':[{'id':u.username,'encryption':'none','flow':flow}]}]},
            'streamSettings':stream,'mux':{'enabled':False}}]}
    except (ValueError, TypeError, AttributeError):
        raise ValueError('VLESS 代理参数无效') from None

@contextmanager
def proxy_url(proxy):
    if not isinstance(proxy, str) or not proxy or len(proxy) > 8192:
        raise ValueError('请先填写账号代理')
    u = urlsplit(proxy)
    if u.scheme in ('http','https','socks5','socks5h') and u.hostname and u.path in ('','/') and not u.query and not u.fragment:
        yield proxy
        return
    if u.scheme != 'vless':
        raise ValueError('OAuth 需要 HTTP、SOCKS5 或 VLESS 代理，不能直连')
    config = vless_config(proxy)
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    helper = root / ('teemerchant-egress.exe' if os.name == 'nt' else 'teemerchant-egress')
    if not helper.is_file():
        raise RuntimeError('缺少 VLESS 组件，请使用完整发行包')
    env = {k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
    env.update(GOMAXPROCS='2', GOMEMLIMIT='128MiB')
    child = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, creationflags=0x08000000 if os.name == 'nt' else 0)
    replies = queue.Queue(maxsize=3)
    def read():
        try:
            while True:
                raw = child.stdout.readline(4097)
                if not raw or len(raw) > 4096: break
                replies.put(json.loads(raw), timeout=1)
        except Exception: pass
    threading.Thread(target=read, daemon=True).start()
    try:
        ready = replies.get(timeout=10)
        if ready.get('ready') is not True or ready.get('version') != 1 or type(ready.get('port')) is not int or not 0 < ready['port'] <= 65535 or not re.fullmatch(r'[0-9a-f]{64}',ready.get('password','')):
            raise RuntimeError()
        route = os.urandom(32).hex()
        child.stdin.write(json.dumps({'op':'add','id':route,'config':config}).encode()+b'\n');child.stdin.flush()
        if replies.get(timeout=10).get('ok') is not True: raise RuntimeError()
        yield f"socks5h://{route}:{ready['password']}@127.0.0.1:{ready['port']}"
    except (queue.Empty, OSError):
        raise OAuthError('VLESS 代理不可用') from None
    finally:
        child.kill();child.wait(timeout=5)
        child.stdin.close();child.stdout.close()

def request(provider, url, proxy, *, data=None, json_body=None):
    official(provider, url)
    with proxy_url(proxy) as effective:
        try:
            with httpx.Client(proxy=effective, timeout=30, trust_env=False, follow_redirects=False) as client:
                with client.stream('GET' if data is None and json_body is None else 'POST', url,
                        data=data, json=json_body, headers={'Accept':'application/json'}) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        raise OAuthError(f'官方授权服务返回 HTTP {status}', permanent=status in (400,401,403), uncertain=status >= 500)
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 256*1024: raise OAuthError('官方授权响应过大', uncertain=True)
                    result = json.loads(raw)
                    if not isinstance(result, dict) or result.get('error'): raise OAuthError('官方授权响应无效', uncertain=True)
                    return result
        except OAuthError: raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise OAuthError('无法连接账号代理或官方授权服务') from None
        except (httpx.HTTPError, ValueError):
            # A POST may already have rotated the token. Never blindly replay it.
            raise OAuthError('授权响应未完整到达，需重新授权确认', uncertain=data is not None or json_body is not None) from None
