"""Use teeproxy's private merchant pipe. Never send a credential through ordinary HTTP."""
import json
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

class Transport:
    def __init__(self, store):
        self.store, self.process = store, None
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                self.process = None

    def start(self):
        settings = self.store.settings
        url = urlsplit(settings['router'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('请填写 Router 的 HTTPS 根地址')
        if not settings['tdx_policy'] and not settings['snp_policy']:
            raise ValueError('请选择独立审核的 TEE 发布策略')
        executable = Path(settings['transport']).resolve(strict=True)
        config = self.store.directory / 'transport.toml'
        values = {'listen': '127.0.0.1:0', 'router': settings['router'], 'router_key_env': 'TEEMERCHANT_UNUSED'}
        for name in ('tdx_policy', 'snp_policy'):
            if settings[name]:
                values[name] = str(Path(settings[name]).resolve(strict=True))
        config.write_text('\n'.join(f'{key} = {json.dumps(value)}' for key, value in values.items()), encoding='utf8')
        self.process = subprocess.Popen([str(executable), 'merchant-transport', '--config', str(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if self.read() != {'ready': True, 'scope': 'merchant'}:
            self.close()
            raise RuntimeError('商户验证组件未就绪，请使用支持商户通道的 teeproxy')

    def read(self):
        process = self.process
        timer = threading.Timer(120, process.kill)
        timer.daemon = True
        timer.start()
        try:
            line = process.stdout.readline(40 * 1024 * 1024 + 1)
            if not line.endswith(b'\n') or len(line) > 40 * 1024 * 1024:
                raise RuntimeError('TEE 管理连接中断；写入结果未知，请查询状态后再重试')
            return json.loads(line)
        finally:
            timer.cancel()

    def call(self, method, path, body=None):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self.start()
            request = {'method': method, 'path': path, 'token': self.store.settings['token'], 'body': body}
            try:
                self.process.stdin.write(json.dumps(request, separators=(',', ':')).encode() + b'\n')
                self.process.stdin.flush()
                result = self.read()
            except (BrokenPipeError, OSError, ValueError):
                self.close()
                raise RuntimeError('TEE 管理连接中断；没有自动重发写入') from None
            if 'error' in result:
                raise RuntimeError('TEE 验证或管理连接失败：' + str(result['error']))
            if result['status'] >= 300:
                error = result['body'].get('error', {})
                raise RuntimeError(f"Router 返回 {result['status']}：{error.get('code', 'request_failed')}")
            return result['body']
