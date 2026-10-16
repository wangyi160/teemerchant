"""Private JSON-lines pipe for Electron. Never starts a web server or opens a port."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading

from app import Application
from desktop_runtime import ChildJob, DataLock
from store import Store
from transport import Transport

MAX_REQUEST = 16 * 1024 * 1024


def serve(application, source, sink):
    output_lock = threading.Lock()
    slots = threading.BoundedSemaphore(8)
    stop = threading.Event()

    def send(message):
        data = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        with output_lock:
            sink.write(data)
            sink.flush()

    def execute(request):
        try:
            result = application.api(request['method'], request['path'], request.get('body', {}))
            send({'id': request['id'], 'result': result})
        except (ValueError, RuntimeError) as error:
            send({'id': request['id'], 'error': str(error)})
        except Exception:
            send({'id': request['id'], 'error': '本地管理操作失败，请检查连接配置'})
        finally:
            slots.release()

    background = threading.Thread(target=application.background, args=(stop,), daemon=True)
    background.start()
    send({'ready': True, 'protocol': 1})
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            while True:
                line = source.readline(MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                    break  # Invalid framing; never attempt to reinterpret trailing bytes.
                try:
                    request = json.loads(line)
                    if (not isinstance(request, dict) or set(request) - {'id', 'method', 'path', 'body'}
                            or type(request.get('id')) is not int or not 1 <= request['id'] <= 2**53-1
                            or request.get('method') not in ('GET', 'POST', 'PUT', 'DELETE')
                            or not isinstance(request.get('path'), str) or len(request['path']) > 4096
                            or not request['path'].startswith('/api/') or not isinstance(request.get('body', {}), dict)):
                        raise ValueError()
                except (ValueError, TypeError):
                    break
                if not slots.acquire(blocking=False):
                    send({'id': request['id'], 'error': '操作较多，请等待当前任务完成'})
                    continue
                executor.submit(execute, request)
    finally:
        stop.set()
        application.transport.close()
        background.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    args = parser.parse_args()
    job = ChildJob()
    lock = DataLock(args.data)
    store = None
    try:
        store = Store(args.data)
        serve(Application(store, Transport(store)), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        if store:
            store.db.close()
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not send tracebacks, credential JSON or filesystem contents to logs.
        sys.stdout.buffer.write(json.dumps({'fatal': '商户工作台无法打开本地数据，请关闭旧版工作台并检查数据目录权限'}, ensure_ascii=False).encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()
        sys.exit(1)
