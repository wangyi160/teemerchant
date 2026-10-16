import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest

from app import Application
from portal_client import portal_origin
from store import Store


class PortalBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.calls = []
        self.mode = 'ok'
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls.append((self.path, body, dict(self.headers)))
                if self.path.endswith('/login'):
                    if fixture.mode == 'redirect':
                        self.send_response(302)
                        self.send_header('Location', fixture.url+'/credential-collector')
                        self.end_headers()
                        return
                    result = {'csrf':'fixture-csrf', 'account':{'role':'user' if fixture.mode=='user' else 'merchant'},
                        'applications':[{'kind':'merchant', 'status':'pending' if fixture.mode=='pending' else 'approved'}]}
                elif self.path.endswith('/bind'):
                    if self.headers.get('Cookie') != 'teerouter_portal=fixture' or self.headers.get('X-Csrf-Token') != 'fixture-csrf':
                        self.send_response(403); self.end_headers(); return
                    result = {'id':fixture.store.enrollment()['id'], 'status':'provisioning', 'router_url':'https://router.example'}
                else: result = {'ok':True}
                self.send_response(200)
                self.send_header('Set-Cookie','teerouter_portal=fixture; Path=/portal; HttpOnly')
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.store.db.close(); self.tmp.cleanup()

    def bind(self):
        return Application(self.store).api('POST','/api/portal/bind',{
            'portal_url':self.url+'/portal/', 'username':'Seller', 'password':'FAKE_PORTAL_PASSWORD'})

    def test_only_public_identity_is_bound_and_password_is_not_persisted(self):
        old = self.store.enrollment()
        result = self.bind()
        self.assertEqual(result['id'],old['id'])
        self.assertEqual([p for p,_,_ in self.calls], ['/portal/api/login','/portal/api/merchant/bind','/portal/api/logout'])
        self.assertEqual(self.calls[1][1],old)
        self.assertEqual(self.calls[0][1]['password'],'FAKE_PORTAL_PASSWORD')
        self.assertNotIn(self.store.settings['token'],json.dumps(self.calls))
        self.assertNotIn('FAKE_PORTAL_PASSWORD',json.dumps(self.store.settings))
        self.assertNotIn('fixture-csrf',json.dumps(self.store.settings))
        self.assertEqual(self.store.settings['portal_username'],'seller')
        self.assertEqual(self.store.settings['router'],'')
        reopened=Store(self.tmp.name)
        self.assertEqual(reopened.enrollment(),old)
        self.assertNotIn('FAKE_PORTAL_PASSWORD',json.dumps(reopened.settings))
        reopened.db.close()

    def test_unapproved_or_user_accounts_cannot_submit_identity(self):
        for mode in ('user','pending'):
            self.calls.clear(); self.mode=mode
            with self.assertRaises(ValueError): self.bind()
            self.assertFalse(any(p.endswith('/bind') for p,_,_ in self.calls))
            self.assertTrue(self.calls[-1][0].endswith('/logout'))
        self.assertNotIn('portal_url',self.store.settings)

    def test_credentials_are_not_forwarded_through_redirects(self):
        self.mode='redirect'
        with self.assertRaises(RuntimeError) as raised: self.bind()
        self.assertNotIn('FAKE_PORTAL_PASSWORD',str(raised.exception))
        self.assertEqual(len(self.calls),1)

    def test_public_plain_http_and_embedded_credentials_are_rejected(self):
        for url in ('http://example.com','https://user:password@example.com','https://example.com/other',
                    'https://example.com/portal/?next=other','http://localhost.evil.example','file:///test','https://example.com:99999'):
            with self.assertRaises(ValueError): portal_origin(url)
        self.assertEqual(portal_origin('https://example.com/portal/'),'https://example.com')
        self.assertEqual(portal_origin(self.url),self.url)
        self.assertEqual(portal_origin('http://[::1]:8080/portal'),'http://[::1]:8080')
