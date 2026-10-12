"""Merchant-owned Windows storage. Secrets and archived records use current-user DPAPI."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading

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
        return {k: v for k, v in self.settings.items() if k != 'token'}

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
