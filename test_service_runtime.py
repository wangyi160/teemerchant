import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from app import Application
from desktop_runtime import DataLock
from service import Runtime, options


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.instances=[]

    def tearDown(self):
        for runtime in self.instances:
            runtime.shutdown()
            self.assertTrue(runtime.done.wait(15))
        self.temp.cleanup()

    def runtime(self,port=None):
        result=Runtime(options(['--headless','--port',str(port or free_port())],self.base))
        self.instances.append(result)
        result.start()
        for _ in range(100):
            if result.state!='starting':break
            time.sleep(.05)
        return result

    def test_http_lifetime_and_exclusive_vault_ownership(self):
        runtime=self.runtime()
        self.assertEqual(runtime.state,'running',runtime.error)
        with httpx.Client(trust_env=False) as client:
            self.assertEqual(client.get(runtime.args.url+'/login').status_code,200)
            self.assertEqual(client.get(runtime.args.url+'/api/state').status_code,401)
        duplicate=self.runtime()
        self.assertTrue(duplicate.done.wait(5))
        self.assertEqual(duplicate.state,'failed')
        self.assertIn('另一个',duplicate.error)
        runtime.shutdown()
        self.assertTrue(runtime.done.wait(5))
        lock=DataLock(runtime.args.data);lock.close()
        self.assertEqual(runtime.state,'stopped')

    def test_occupied_port_is_not_reported_as_running(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen()
            runtime=self.runtime(sock.getsockname()[1])
            self.assertTrue(runtime.done.wait(5))
            self.assertEqual(runtime.state,'failed')
            self.assertIn('已被占用',runtime.error)
        lock=DataLock(runtime.args.data);lock.close()

    def test_stop_waits_for_renewal_before_releasing_vault(self):
        entered=threading.Event();release=threading.Event()
        def background(_application,stop):
            entered.set();release.wait(10)
        with patch.object(Application,'background',background):
            runtime=self.runtime()
            try:
                self.assertTrue(entered.wait(2))
                runtime.shutdown()
                self.assertFalse(runtime.done.wait(.5))
                with self.assertRaises(RuntimeError):DataLock(runtime.args.data)
                self.assertEqual(runtime.state,'stopping')
            finally:release.set()
            self.assertTrue(runtime.done.wait(5))

    def test_stop_during_startup_closes_everything(self):
        runtime=Runtime(options(['--port',str(free_port())],self.base))
        self.instances.append(runtime)
        runtime.shutdown();runtime.start()
        self.assertTrue(runtime.done.wait(5))
        self.assertEqual(runtime.state,'stopped',runtime.error)
        lock=DataLock(runtime.args.data);lock.close()

    def test_shutdown_drains_in_flight_http_before_unlocking_data(self):
        from http.server import BaseHTTPRequestHandler
        entered=threading.Event();release=threading.Event()
        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                entered.set();release.wait(10)
                self.send_response(204);self.end_headers()
        with patch('service.handler',return_value=SlowHandler):
            runtime=self.runtime()
            errors=[]
            def request():
                try:
                    with httpx.Client(trust_env=False,timeout=10) as client:
                        self.assertEqual(client.get(runtime.args.url).status_code,204)
                except Exception as error:errors.append(error)
            client=threading.Thread(target=request);client.start()
            try:
                self.assertTrue(entered.wait(2))
                runtime.shutdown()
                self.assertFalse(runtime.done.wait(.5))
                with self.assertRaises(RuntimeError):DataLock(runtime.args.data)
            finally:release.set();client.join(10)
            self.assertTrue(runtime.done.wait(5))
            self.assertFalse(errors,errors)

    def test_portable_config_and_command_line_override(self):
        (self.base/'service.json').write_text(json.dumps({'host':'0.0.0.0','port':8910,
            'public_urls':['http://192.0.2.10:8910'],'data':'merchant-data'}))
        args=options([],self.base)
        self.assertEqual(args.url,'http://192.0.2.10:8910')
        self.assertEqual(Path(args.data),self.base/'merchant-data')
        args=options(['--port','8911','--public-url','http://192.0.2.10:8911'],self.base)
        self.assertEqual(args.port,8911)
        self.assertEqual(args.public_url,['http://192.0.2.10:8911'])
        self.assertEqual(args.url,'http://192.0.2.10:8911')


if __name__=='__main__':unittest.main()
