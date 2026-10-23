"""Merchant-owned encrypted storage for Windows and Linux."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import tempfile
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

def protector(directory):
    if os.name == 'nt':
        return protect
    from secure_storage import LinuxVault
    return LinuxVault(directory)

class Store:
    def __init__(self, directory, *, _profile=False):
        self.root_directory = Path(directory).resolve()
        self.directory = self.root_directory
        self.protect = protector(self.directory)
        selection = self.root_directory / 'active-merchant.json'
        if not _profile and selection.exists():
            identity = json.loads(selection.read_text(encoding='utf-8'))['merchant_id']
            self.directory = self._profile_directory(identity)
            if not (self.directory / 'settings.dpapi').is_file():
                raise ValueError('当前商户资料缺失，请恢复完整 data 目录')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.protect = protector(self.directory)
        self.lock = threading.RLock()
        self.path = self.directory / 'settings.dpapi'
        if self.path.exists():
            self.settings = json.loads(self.protect(self.path.read_bytes(), True))
        else:
            self.settings = {'token': secrets.token_hex(32), 'router': 'https://router.teerouter.org', 'platform_url':'https://teerouter.org', 'tdx_policy': '', 'snp_policy': '', 'minimum_revision': 0, 'credentials_saved': False}
            self.save()
        if 'transport' in self.settings:
            self.settings.pop('transport')
            self.save()
        if not _profile and selection.exists() and self.enrollment()['id'] != identity:
            raise ValueError('当前商户资料的身份不一致')
        self.accounts = AccountBook(self.directory, self.enrollment()['id'], self.protect)
        self.db = sqlite3.connect(self.directory / 'records.db', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT, instance TEXT, seq INTEGER, payload BLOB NOT NULL, PRIMARY KEY(kind,instance,seq))')
        self.db.execute('CREATE TABLE IF NOT EXISTS platform_records(url TEXT, archive_id TEXT, seq INTEGER, payload BLOB NOT NULL, PRIMARY KEY(url,seq))')
        self.db.execute('CREATE TABLE IF NOT EXISTS platform_cursors(url TEXT PRIMARY KEY, archive_id TEXT NOT NULL, cursor INTEGER NOT NULL, synced_at INTEGER NOT NULL)')
        self.db.commit()
        self.revenue_index = RevenueIndex()

    def _profile_directory(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'm_[a-f0-9]{32}', identity):
            raise ValueError('商户 ID 无效')
        legacy = self.root_directory / 'settings.dpapi'
        if legacy.exists():
            settings = json.loads(protector(self.root_directory)(legacy.read_bytes(), True))
            if 'm_' + hashlib.sha256(settings['token'].encode()).hexdigest()[:32] == identity:
                return self.root_directory
        directory = (self.root_directory / 'merchants' / identity).resolve()
        if not directory.is_relative_to(self.root_directory):
            raise ValueError('商户资料路径无效')
        return directory

    def profiles(self):
        """Only identity metadata is exposed; each vault and ledger stays private."""
        with self.lock:
            paths = [self.root_directory / 'settings.dpapi']
            paths.extend(sorted((self.root_directory / 'merchants').glob('m_*/settings.dpapi')))
            result = []
            for path in paths:
                if not path.exists():
                    continue
                settings = json.loads(protector(path.parent)(path.read_bytes(), True))
                if not settings.get('credentials_saved', bool(settings.get('token'))):
                    continue
                identity = 'm_' + hashlib.sha256(settings['token'].encode()).hexdigest()[:32]
                if path.parent != self._profile_directory(identity):
                    raise ValueError('已保存的商户资料身份不一致')
                account = settings.get('platform_identity', {})
                if account.get('platform_url') != settings.get('platform_url', '').rstrip('/'):
                    account = {}
                result.append({'merchant_id': identity, 'active': identity == self.enrollment()['id'],
                               'username': account.get('username', ''), 'name': account.get('name', '')})
            return result

    def platform_identity(self):
        with self.lock:
            account = self.settings.get('platform_identity', {})
            if account.get('platform_url') != self.settings.get('platform_url', '').rstrip('/'):
                return {}
            return dict(account)

    def remember_platform_identity(self, base, merchant_id, account, *, legacy=False):
        """Cache identity received from the authenticated platform, per merchant vault."""
        with self.lock:
            if (merchant_id != self.enrollment()['id'] or not self.credentials()['saved']
                    or base != self.settings.get('platform_url', '').rstrip('/')
                    or not isinstance(account, dict) or account.get('role') != 'merchant'
                    or not isinstance(account.get('id'), str)
                    or not isinstance(account.get('name'), str)
                    or (not legacy and not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', account.get('username', '')))):
                raise ValueError('平台返回的商户身份不匹配')
            identity = {'platform_url': base, 'account_id': account['id'],
                        'username': '' if legacy else account['username'],
                        'name': account['name'], 'legacy': bool(legacy)}
            if self.settings.get('platform_identity') == identity:
                return
            previous = self.settings.copy()
            self.settings['platform_identity'] = identity
            try:
                self.save()
            except Exception:
                self.settings = previous
                raise

    def _activate(self, candidate):
        """Commit the selection only after the target vault and database open.

        Existing root-layout data remains untouched. A crash before the atomic
        pointer write retains the old identity; afterwards restart opens the new
        one. No credentials or pending changes are moved between identities.
        """
        try:
            selection = self.root_directory / 'active-merchant.json'
            tmp = selection.with_suffix('.tmp')
            with tmp.open('w', encoding='utf-8') as file:
                json.dump({'merchant_id': candidate.enrollment()['id']}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, selection)
        except Exception:
            candidate.db.close()
            raise
        previous_db = self.db
        for field in ('directory', 'path', 'settings', 'accounts', 'db', 'revenue_index', 'protect'):
            setattr(self, field, getattr(candidate, field))
        previous_db.close()

    def switch_profile(self, body):
        if not isinstance(body, dict) or set(body) != {'merchant_id'}:
            raise ValueError('请选择已保存的商户')
        with self.lock:
            directory = self._profile_directory(body['merchant_id'])
            if not (directory / 'settings.dpapi').is_file():
                raise ValueError('本机尚未保存该商户，请先添加接入凭证')
            if body['merchant_id'] == self.enrollment()['id']:
                return
            candidate = Store(directory, _profile=True)
            if candidate.enrollment()['id'] != body['merchant_id'] or not candidate.credentials()['saved']:
                candidate.db.close()
                raise ValueError('已保存的商户资料身份不一致')
            self._activate(candidate)

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix('.tmp')
            with tmp.open('wb') as file:
                file.write(self.protect(json.dumps(self.settings).encode()))
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)

    def enrollment(self):
        digest = hashlib.sha256(self.settings['token'].encode()).hexdigest()
        return {'id': 'm_' + digest[:32], 'credential_hash': digest}

    def public(self):
        return {k: v for k, v in self.settings.items() if k not in ('token', 'owner_secret', 'default_multiplier_millionths')}

    def control_token(self):
        """Persist an independent owner key before the first attested binding."""
        with self.lock:
            owner = self.settings.get('owner_secret')
            if owner is None:
                owner = secrets.token_hex(32)
                self.settings['owner_secret'] = owner
                try:
                    self.save()
                except Exception:
                    self.settings.pop('owner_secret', None)
                    raise
            if not re.fullmatch(r'[a-f0-9]{64}', owner) or owner == self.settings['token']:
                raise ValueError('商户本机控制密钥无效')
            return self.settings['token'] + '.' + owner

    def credentials(self, reveal=False):
        with self.lock:
            # Older profiles already held locally generated/enrolled or imported
            # credentials. Preserve those; a fresh profile needs Portal credentials.
            saved = bool(self.settings.get('credentials_saved', bool(self.settings.get('token'))))
            result = {'saved': saved, 'merchant_id': self.enrollment()['id'] if saved else ''}
            if reveal:
                result['merchant_secret'] = self.settings['token'] if saved else ''
            return result

    def policy_path(self, value):
        path = Path(value)
        if path.is_absolute():
            return path.resolve(strict=True)
        resolved = (self.directory / path).resolve(strict=True)
        if not resolved.is_relative_to(self.directory):
            raise ValueError('策略相对路径必须位于 data 目录中')
        return resolved

    def save_connection(self, body):
        if (not isinstance(body, dict) or set(body) != {'router', 'tdx_policy', 'snp_policy'}
                or any(not isinstance(v, str) or len(v) > 4096 for v in body.values())):
            raise ValueError('连接设置无效')
        with self.lock:
            settings = dict(body)
            for kind in ('tdx_policy', 'snp_policy'):
                if not body[kind]:
                    continue
                try:
                    with self.policy_path(body[kind]).open('rb') as stream:
                        raw = stream.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024 or not isinstance(json.loads(raw), dict):
                        raise ValueError()
                except (OSError, ValueError):
                    raise ValueError('无法读取 TEE 策略，请选择有效的 JSON 策略文件') from None
                # Preserve exact approved bytes. Immutable names plus an atomic
                # settings write keep the previous trust policy on any failure.
                relative = Path('policies') / (kind + '-' + hashlib.sha256(raw).hexdigest() + '.json')
                target = self.directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.read_bytes() != raw:
                    temporary = target.with_suffix('.tmp')
                    with temporary.open('wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                settings[kind] = relative.as_posix()
            previous = self.settings.copy()
            self.settings.update(settings)
            try:
                self.save()
            except Exception:
                self.settings = previous
                raise

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
            # Only the initial, unused placeholder identity can be replaced in
            # place. Saved merchants always keep their own encrypted files.
            if not same and (self.credentials()['saved'] or self.accounts.path.exists() or self.accounts.data['routers']
                             or self.settings['minimum_revision']
                             or self.db.execute('SELECT 1 FROM records LIMIT 1').fetchone()):
                directory = self._profile_directory(body['merchant_id'])
                if (directory / 'settings.dpapi').exists():
                    self.switch_profile({'merchant_id': body['merchant_id']})
                    return
                directory.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.preparing-', dir=directory.parent) as temporary:
                    candidate = Store(temporary, _profile=True)
                    try:
                        candidate.import_credentials(body)
                        # Reuse the approved connection, copying exact policy
                        # bytes. Revision, accounts and archives start empty.
                        connection = {k: self.settings[k] for k in ('router', 'tdx_policy', 'snp_policy')}
                        candidate.settings['platform_url'] = self.settings.get('platform_url', '')
                        for key in ('tdx_policy', 'snp_policy'):
                            if connection[key]:
                                connection[key] = str(self.policy_path(connection[key]))
                        candidate.save_connection(connection)
                    finally:
                        candidate.db.close()
                    os.replace(temporary, directory)
                self._activate(Store(directory, _profile=True))
                return
            previous = self.settings.copy()
            self.settings['token'] = body['merchant_secret']
            if not same:
                self.settings.pop('owner_secret', None)
                self.settings.pop('platform_identity', None)
            self.settings['credentials_saved'] = True
            self.settings.pop('portal_url', None)
            self.settings.pop('portal_username', None)
            try:
                self.save()
            except Exception:
                self.settings = previous
                raise
            if not same:
                self.accounts = AccountBook(self.directory, body['merchant_id'], self.protect)

    def archive(self, kind, instance, rows):
        with self.lock, self.db:
            for row in rows:
                seq = row['seq'] if kind == 'usage' else row['id']
                raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                old = self.db.execute('SELECT payload FROM records WHERE kind=? AND instance=? AND seq=?', (kind, instance, seq)).fetchone()
                if old:
                    if self.protect(old[0], True) != raw:
                        raise RuntimeError('Archived record changed; synchronization stopped')
                    continue
                self.db.execute('INSERT INTO records VALUES(?,?,?,?)', (kind, instance, seq, self.protect(raw)))

    def records(self, kind):
        with self.lock:
            return [json.loads(self.protect(row[0], True)) for row in self.db.execute('SELECT payload FROM records WHERE kind=? ORDER BY rowid DESC LIMIT 200', (kind,))]

    def revenue(self, query=''):
        with self.lock:
            # Every archived usage record contributes, including records older than
            # the activity screen's 200-row limit. Only new rows are decrypted again.
            for rowid, instance, seq, payload in self.db.execute(
                    "SELECT rowid,instance,seq,payload FROM records WHERE kind='usage' AND rowid>? ORDER BY rowid",
                    (self.revenue_index.cursor,)):
                row = json.loads(self.protect(payload, True))
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
            return exact(json.loads(self.protect(row[0], True)))
