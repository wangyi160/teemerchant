"""Merchant-only attested TLS, implemented in-process by teemerchant's native library."""
import ctypes
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit


def library_path():
    name = 'teemerchant_attestation.dll' if sys.platform == 'win32' else 'libteemerchant_attestation.so'
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / 'native' / 'target' / 'release' / name


class NativeClient:
    def __init__(self, config):
        self.handle = 0
        try:
            self.library = ctypes.CDLL(str(library_path().resolve(strict=True)))
        except OSError:
            raise RuntimeError('内置 TEE 模块无法加载，请重新解压完整的 teemerchant ZIP') from None
        lib = self.library
        lib.tm_abi_version.argtypes, lib.tm_abi_version.restype = [], ctypes.c_uint32
        lib.tm_create.argtypes, lib.tm_create.restype = [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_void_p
        lib.tm_request.argtypes, lib.tm_request.restype = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_void_p
        lib.tm_close.argtypes, lib.tm_close.restype = [ctypes.c_uint64], None
        lib.tm_free_response.argtypes, lib.tm_free_response.restype = [ctypes.c_void_p], None
        if lib.tm_abi_version() != 1:
            raise RuntimeError('内置 TEE 模块版本不匹配，请重新解压完整的 teemerchant ZIP')
        result = self.invoke(lib.tm_create, config)
        if type(result.get('handle')) is not int or result['handle'] <= 0:
            raise RuntimeError('Router 地址或本地 TEE 信任策略无效，请选择经过审核的发布策略')
        self.handle = result['handle']

    def invoke(self, function, body, *prefix):
        raw = json.dumps(body, separators=(',', ':')).encode('utf-8')
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError('商户请求过大')
        buffer = ctypes.create_string_buffer(raw)
        try:
            pointer = function(*prefix, buffer, len(raw))
            if not pointer:
                raise RuntimeError('内置 TEE 模块响应无效')
            try:
                result = json.loads(ctypes.string_at(pointer))
                if not isinstance(result, dict):
                    raise RuntimeError('内置 TEE 模块响应无效')
                return result
            finally:
                self.library.tm_free_response(pointer)
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def request(self, body):
        return self.invoke(self.library.tm_request, body, self.handle)

    def close(self):
        if self.handle:
            self.library.tm_close(self.handle)
            self.handle = 0


class Transport:
    def __init__(self, store):
        self.store, self.client = store, None
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            if self.client:
                self.client.close()
                self.client = None

    def start(self):
        settings = self.store.settings
        url = urlsplit(settings['router'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('请填写 Router 的 HTTPS 根地址')
        if not settings['tdx_policy'] and not settings['snp_policy']:
            raise ValueError('请选择独立审核的 TEE 发布策略')
        config = {'endpoint': settings['router']}
        for name in ('tdx_policy', 'snp_policy'):
            config[name] = str(self.store.policy_path(settings[name])) if settings[name] else None
        self.client = NativeClient(config)

    def call(self, method, path, body=None):
        if urlsplit(path).path in ('/merchant/usage', '/merchant/usage/ack', '/merchant/refusals'):
            raise ValueError('流水和拒答记录请从管理平台获取')
        def check(value):
            if isinstance(value, dict):
                if any(str(k).replace('_', '').lower() == 'refreshtoken' for k in value):
                    raise ValueError('协议禁止向 Router 发送 refresh token')
                for item in value.values(): check(item)
            elif isinstance(value, list):
                for item in value: check(item)
        check(body)
        with self.lock:
            if self.client is None:
                self.start()
            try:
                result = self.client.request({'method':method,'path':path,'token':self.store.control_token(),'body':body})
            except Exception:
                self.close()
                raise RuntimeError('TEE 管理连接失败；没有自动重发写入，请查询状态后再重试') from None
            if 'error' in result:
                self.close()
                raise RuntimeError('TEE 验证或管理连接失败：' + str(result['error']))
            if result['status'] >= 300:
                error = result['body'].get('error', {})
                raise RuntimeError(f"Router 返回 {result['status']}：{error.get('code', 'request_failed')}")
            return result['body']
