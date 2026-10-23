"""Run the actual self-contained distribution with isolated data; no live account mutations."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import unittest
import httpx

ROOT=Path(__file__).parent
EXE=ROOT/'build/service/teemerchant'/('teemerchant.exe' if os.name=='nt' else 'teemerchant')

@unittest.skipUnless(EXE.is_file(),'Build the portable service first')
class PortableTests(unittest.TestCase):
    def test_portable_local_page_native_load_and_data_lock(self):
        with tempfile.TemporaryDirectory(prefix='teemerchant-service-test-') as folder:
            with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            data=Path(folder)/'data'
            log=Path(folder)/'service.log'
            with log.open('wb') as output:
                child=subprocess.Popen([str(EXE),'--headless','--port',str(port),'--data',str(data)],stdout=output,stderr=output,
                    creationflags=0x08000000 if os.name=='nt' else 0)
                try:
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=10) as client:
                        for _ in range(100):
                            try:page=client.get('/');break
                            except httpx.ConnectError:time.sleep(.1)
                        else:raise AssertionError(log.read_text())
                        self.assertEqual(page.status_code,200)
                        self.assertEqual(client.get('/api/state').status_code,403)
                        csrf=re.search('name="csrf" content="([^"]+)"',page.text)[1]
                        self.assertFalse((data/'initial-password.txt').exists())
                        self.assertFalse((data/'http-auth.json').exists())
                        headers={'X-CSRF-Token':csrf}
                        state=client.get('/api/state',headers=headers).json()
                        self.assertFalse(state['connected']);self.assertFalse(state['credentials']['saved'])
                        self.assertEqual(client.get('/app.js').status_code,200)
                        self.assertIn('window.teemerchant',client.get('/app.js').text)
                        account=client.get('/account/login')
                        self.assertEqual(account.status_code,200)
                        self.assertIn('name="local-csrf" content="'+csrf+'"',account.text)
                        self.assertNotIn('LOCAL_CSRF',account.text)
                        self.assertEqual(client.get('/account-assets/withdrawals.js').status_code,200)
                        self.assertEqual(client.get('/account-assets/../../settings.dpapi').status_code,403)
                        self.assertEqual(client.post('/account-api/login',json={}).status_code,403)
                        # Exercise the packaged C ABI using an invalid policy. Missing
                        # DLL/SO is a packaging failure, a policy rejection is expected.
                        policy=data/'empty-policy.json';policy.write_text('{"releases":[]}')
                        headers['X-Merchant-ID']=state['enrollment']['id']
                        saved=client.put('/api/settings',headers=headers,json={'router':'https://fixture.invalid','tdx_policy':str(policy),'snp_policy':''})
                        self.assertEqual(saved.status_code,200)
                        result=client.post('/api/verify',headers=headers,json={})
                        self.assertEqual(result.status_code,400)
                        self.assertNotIn('无法加载',result.text)
                    another=subprocess.run([str(EXE),'--headless','--port',str(port+1),'--data',str(data)],capture_output=True,timeout=15)
                    self.assertNotEqual(another.returncode,0)
                finally:
                    child.terminate();child.wait(timeout=20)
            self.assertTrue((data/'settings.dpapi').exists())
            self.assertFalse(any(p.name.startswith('electron') for p in EXE.parent.rglob('*')))

if __name__=='__main__':unittest.main()
