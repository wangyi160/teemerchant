"""Merchant-owned Windows storage. Secrets and archived records use current-user DPAPI."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import threading
from local_accounts import AccountBook
from revenue import RevenueIndex

class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

def protect(value, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('This build requires Windows DPAPI')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source, target = Blob(len(value), buf), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows credential encryption failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)

class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.directory / 'settings.dpapi'
        if self.path.exists():
            self.settings = json.loads(protect(self.path.read_bytes(), True))
        else:
            self.settings = {'token': secrets.token_hex(32), 'router': '', 'tdx_policy': '', 'snp_policy': '', 'transport': '', 'minimum_revision': 0}
            self.save()
        self.db = sqlite3.connect(self.directory / 'records.db', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT, instance TEXT, seq INTEGER, payload BLOB NOT NULL, PRIMARY KEY(kind,instance,seq))')
        self.db.commit()
        self.revenue_index = RevenueIndex()
        self.accounts = AccountBook(self.directory, self.enrollment()['id'], protect)

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix('.tmp')
            with tmp.open('wb') as file:
                file.write(protect(json.dumps(self.settings).encode()))
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)

    def enrollment(self):
        digest = hashlib.sha256(self.settings['token'].encode()).hexdigest()
        return {'id': 'm_' + digest[:32], 'credential_hash': digest}

    def public(self):
        return {k: v for k, v in self.settings.items() if k not in ('token', 'default_multiplier_millionths')}

    def import_credentials(self, body):
        if (not isinstance(body, dict) or set(body) != {'merchant_id', 'merchant_secret'}
                or not all(isinstance(v, str) for v in body.values())
                or not re.fullmatch(r'm_[a-f0-9]{32}', body['merchant_id'])
                or not re.fullmatch(r'[a-f0-9]{64}', body['merchant_secret'])):
            raise ValueError('请填写平台签发的 Merchant ID 和 Merchant Secret')
        digest = hashlib.sha256(body['merchant_secret'].encode()).hexdigest()
        if not secrets.compare_digest(body['merchant_id'], 'm_' + digest[:32]):
            raise ValueError('Merchant ID 与 Merchant Secret 不匹配')
        with self.lock:
            same = secrets.compare_digest(self.settings['token'], body['merchant_secret'])
            # A new empty profile has no account file. Changing only the atomic
            # settings file makes initial import crash-safe; never rewrite a vault.
            if not same and (self.accounts.path.exists() or self.accounts.data['routers']
                             or self.settings['minimum_revision']
                             or self.db.execute('SELECT 1 FROM records LIMIT 1').fetchone()):
                raise ValueError('本机已有商户资料，不能替换为其他商户身份；请使用原 Merchant ID 和 Merchant Secret')
            previous = self.settings.copy()
            self.settings['token'] = body['merchant_secret']
            self.settings.pop('portal_url', None)
            self.settings.pop('portal_username', None)
            try:
                self.save()
            except Exception:
                self.settings = previous
                raise
            if not same:
                self.accounts = AccountBook(self.directory, body['merchant_id'], protect)

    def archive(self, kind, instance, rows):
        with self.lock, self.db:
            for row in rows:
                seq = row['seq'] if kind == 'usage' else row['id']
                raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                old = self.db.execute('SELECT payload FROM records WHERE kind=? AND instance=? AND seq=?', (kind, instance, seq)).fetchone()
                if old:
                    if protect(old[0], True) != raw:
                        raise RuntimeError('Archived record changed; synchronization stopped')
                    continue
                self.db.execute('INSERT INTO records VALUES(?,?,?,?)', (kind, instance, seq, protect(raw)))

    def records(self, kind):
        with self.lock:
            return [json.loads(protect(row[0], True)) for row in self.db.execute('SELECT payload FROM records WHERE kind=? ORDER BY rowid DESC LIMIT 200', (kind,))]

    def revenue(self, query=''):
        with self.lock:
            # Every archived usage record contributes, including records older than
            # the activity screen's 200-row limit. Only new rows are decrypted again.
            for rowid, instance, seq, payload in self.db.execute(
                    "SELECT rowid,instance,seq,payload FROM records WHERE kind='usage' AND rowid>? ORDER BY rowid",
                    (self.revenue_index.cursor,)):
                row = json.loads(protect(payload, True))
                event = row.get('event', {})
                self.revenue_index.add(rowid, instance, seq, event)
            return self.revenue_index.report(query)

    def revenue_record(self, instance, seq):
        with self.lock:
            row = self.db.execute("SELECT payload FROM records WHERE kind='usage' AND instance=? AND seq=?", (instance, seq)).fetchone()
            if not row:
                raise ValueError('收入记录不存在')
            def exact(value):
                if isinstance(value, dict):
                    return {k: exact(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [exact(v) for v in value]
                return str(value) if type(value) is int and abs(value) > 2**53-1 else value
            return exact(json.loads(protect(row[0], True)))
