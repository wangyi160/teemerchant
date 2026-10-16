'use strict';
const {spawn} = require('node:child_process');
const {EventEmitter} = require('node:events');
const MAX = 40 * 1024 * 1024;

function validateRequest(method, path, body = {}, identity) {
  const fixed = new Set(['GET /api/state', 'PUT /api/settings', 'POST /api/verify', 'POST /api/connect',
    'GET /api/merchant', 'PUT /api/config', 'POST /api/config/sync', 'POST /api/refresh',
    'POST /api/sync', 'GET /api/records/usage', 'GET /api/records/refusals', 'GET /api/credentials', 'POST /api/credentials', 'POST /api/merchant-profile']);
  if (typeof method !== 'string' || typeof path !== 'string' || path.length > 4096 || !body ||
      typeof body !== 'object' || Array.isArray(body)) throw Error('无效的桌面操作');
  if (!fixed.has(`${method} ${path}`) && !(method === 'GET' &&
      (/^\/api\/accounts\/[A-Za-z0-9_.%-]+$/.test(path) || /^\/api\/revenue(?:\/record)?(?:\?[^#\r\n]*)?$/.test(path)))) {
    throw Error('桌面接口未授权');
  }
  if (Buffer.byteLength(JSON.stringify(body), 'utf8') > 15 * 1024 * 1024) throw Error('请求过大');
  if(identity!==undefined && (typeof identity!=='string'||!/^m_[a-f0-9]{32}$/.test(identity)))throw Error('商户身份无效');
  return {method, path, body, ...(identity===undefined?{}:{identity})};
}

class Backend extends EventEmitter {
  constructor(executable, args, options = {}) {
    super();
    this.pending = new Map(); this.sequence = 0; this.buffer = Buffer.alloc(0); this.stopping = false;
    this.ready = new Promise((resolve, reject) => { this.readyResolve = resolve; this.readyReject = reject; });
    const env = {...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8'};
    delete env.PYTHONPATH; delete env.PYTHONHOME;
    this.child = spawn(executable, args, {windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'], ...options, env});
    this.timer = setTimeout(() => this.fail('本地商户组件启动超时'), 30000);
    this.child.stdout.on('data', chunk => this.receive(chunk));
    this.child.stdin.on('error', () => this.fail('本地管理连接中断；请重启应用并核对同步状态'));
    this.child.on('error', () => this.fail('无法启动本地商户组件'));
    this.child.on('exit', () => this.fail('本地商户组件已退出；未自动重发操作'));
  }
  receive(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const end = this.buffer.indexOf(10);
      if (end < 0) break;
      if (end > MAX) return this.fail('本地组件响应过大');
      const line = this.buffer.subarray(0, end); this.buffer = this.buffer.subarray(end + 1);
      let value;
      try { value = JSON.parse(line.toString('utf8')); } catch { return this.fail('本地组件响应无效'); }
      if (value.fatal) return this.fail(String(value.fatal));
      if (value.ready === true && value.protocol === 1) {
        clearTimeout(this.timer); this.readyResolve(); continue;
      }
      const request = this.pending.get(value.id);
      if (!request) continue;
      this.pending.delete(value.id); clearTimeout(request.timer);
      if (typeof value.error === 'string') request.reject(Error(value.error));
      else request.resolve(value.result);
    }
    if (this.buffer.length > MAX) this.fail('本地组件响应过大');
  }
  fail(message) {
    clearTimeout(this.timer); this.readyReject(Error(message));
    for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(Error(message)); }
    this.pending.clear();
    if (!this.stopping) { this.stopping = true; this.child.kill(); this.emit('unavailable', message); }
  }
  async call(method, path, body = {}, identity) {
    const request = validateRequest(method, path, body, identity);
    await this.ready;
    if (this.stopping) throw Error('本地组件已退出，请重启应用');
    if (this.pending.size >= 8) throw Error('操作较多，请等待当前任务完成');
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Error('操作尚未确认，请刷新状态；没有自动重发写入'));
      }, 150000);
      this.pending.set(id, {resolve, reject, timer});
      this.child.stdin.write(JSON.stringify({id, ...request}) + '\n');
    });
  }
  async close() {
    this.stopping = true;
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    await new Promise(resolve => {
      const timer = setTimeout(() => { this.child.kill(); resolve(); }, 8000);
      this.child.once('exit', () => { clearTimeout(timer); resolve(); });
      this.child.stdin.end();
    });
  }
}
module.exports = {Backend, validateRequest};
