"""Exercise the actual Windows message loop with an isolated HTTP service."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from desktop_runtime import DataLock
from test_service_runtime import free_port


@unittest.skipUnless(os.name=='nt','Native Windows UI')
class StatusWindowTests(unittest.TestCase):
    def setUp(self):
        from windows_status import api, class_name
        self.temp=tempfile.TemporaryDirectory(prefix='merchant-window-')
        self.data=Path(self.temp.name)/'data'
        self.port=free_port()
        executable=os.environ.get('TEEMERCHANT_TEST_EXE')
        self.command=[executable] if executable else [sys.executable,str(Path(__file__).parent/'service.py')]
        self.command+=['--data',str(self.data),'--port',str(self.port)]
        self.user,self.kernel,_,_=api()
        self.user.GetDlgItem.argtypes=[W.HWND,C.c_int];self.user.GetDlgItem.restype=W.HWND
        self.user.IsWindowVisible.argtypes=[W.HWND];self.user.IsWindowVisible.restype=W.BOOL
        self.user.IsWindowEnabled.argtypes=[W.HWND];self.user.IsWindowEnabled.restype=W.BOOL
        self.user.GetWindowTextW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
        self.name=class_name(self.data)
        self.process=None
        self.window=None
        self.log=(Path(self.temp.name)/'process.log').open('wb')

    def tearDown(self):
        if self.process:
            if self.process.poll() is None:
                if self.window:self.user.PostMessageW(self.window,0x111,205,0)
                try:self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.log.close()
        self.temp.cleanup()

    def wait_for(self,condition):
        for _ in range(200):
            if condition():return
            if self.process.poll() is not None:
                self.fail((Path(self.temp.name)/'process.log').read_text(errors='replace'))
            time.sleep(.05)
        self.fail('Windows UI condition did not become true')

    def start(self):
        self.process=subprocess.Popen(self.command,cwd=self.temp.name,stdout=self.log,stderr=self.log,creationflags=0x08000000)
        def found():
            self.window=self.user.FindWindowW(self.name,None)
            return bool(self.window and self.user.GetDlgItem(self.window,102))
        self.wait_for(found)

    def text(self,identifier):
        buffer=C.create_unicode_buffer(2048)
        self.user.GetWindowTextW(self.user.GetDlgItem(self.window,identifier),buffer,len(buffer))
        return buffer.value

    def test_minimize_close_restore_and_single_instance_keep_http_alive(self):
        self.start()
        self.wait_for(lambda:self.text(102)=='服务运行中')
        url=f'http://127.0.0.1:{self.port}'
        with httpx.Client(trust_env=False,timeout=5) as client:
            self.assertEqual(client.get(url+'/api/state').status_code,401)
            self.assertEqual(client.get(url+'/').status_code,200)
            self.user.PostMessageW(self.window,0x112,0xf020,0)
            self.wait_for(lambda:not self.user.IsWindowVisible(self.window))
            self.assertEqual(client.get(url+'/').status_code,200)
            duplicate=subprocess.run(self.command,cwd=self.temp.name,capture_output=True,creationflags=0x08000000,timeout=15)
            self.assertEqual(duplicate.returncode,0,duplicate.stderr)
            self.wait_for(lambda:self.user.IsWindowVisible(self.window))
            self.user.PostMessageW(self.window,0x10,0,0)
            self.wait_for(lambda:not self.user.IsWindowVisible(self.window))
            self.assertEqual(client.get(url+'/').status_code,200)
            # Notification-area activation follows the v4 shell callback format.
            self.user.PostMessageW(self.window,0x8001,0,(1<<16)|0x203)
            self.wait_for(lambda:self.user.IsWindowVisible(self.window))
            self.assertTrue(self.user.IsWindowEnabled(self.user.GetDlgItem(self.window,201)))
        self.user.PostMessageW(self.window,0x111,205,0)
        self.assertEqual(self.process.wait(timeout=15),0)
        lock=DataLock(self.data);lock.close()
        with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',self.port)),0)

    def test_port_conflict_is_visible_and_does_not_offer_open(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',self.port));sock.listen()
            self.start()
            self.wait_for(lambda:self.text(102)=='服务未运行' and not self.user.IsWindowEnabled(self.user.GetDlgItem(self.window,201)))
            self.assertFalse(self.user.IsWindowEnabled(self.user.GetDlgItem(self.window,201)))
            self.user.PostMessageW(self.window,0x111,205,0)
            self.assertEqual(self.process.wait(timeout=15),1)


if __name__=='__main__':unittest.main()
