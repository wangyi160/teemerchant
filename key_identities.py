"""Client-owned merchant identities. Private seeds never reach the HTTP UI or platform."""
import hashlib
import json
import re
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from remembered_login import SessionFile


class Identities:
    def __init__(self, directory):
        self.directory = Path(directory) / 'identities'

    def file(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'm_sig_[a-f0-9]{64}', identity):
            raise ValueError('身份 ID 无效')
        return SessionFile(self.directory, identity + '.dpapi')

    def create(self, base, account_id=None):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        identity = 'm_sig_' + hashlib.sha256(bytes.fromhex(public)).hexdigest()
        value = {'identity_id': identity, 'platform': base, 'public_key': public,
                 'seed': key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex(),
                 'account_id': account_id}
        self.file(identity).write(value)
        return value

    def load(self, base, identity):
        value = self.file(identity).read()
        if value.get('platform') != base or value.get('identity_id') != identity:
            raise ValueError('身份文件或平台不匹配')
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value['seed']))
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if public.hex() != value['public_key'] or identity != 'm_sig_' + hashlib.sha256(public).hexdigest():
            raise ValueError('身份文件损坏')
        return value

    def list(self, base):
        result = []
        for path in sorted(self.directory.glob('m_sig_*.dpapi')):
            try:
                value = self.load(base, path.stem)
                result.append({k: value.get(k) for k in ('identity_id', 'public_key', 'account_id')})
            except (ValueError, KeyError, OSError):
                continue
        return result

    def for_account(self, base, account_id):
        for row in self.list(base):
            if row['account_id'] == account_id:
                return self.load(base, row['identity_id'])
        return self.create(base, account_id)

    def bind(self, base, identity, account_id):
        value = self.load(base, identity)
        if account_id != identity:
            raise ValueError('账户 ID 必须与本地公钥身份一致')
        if value.get('account_id') not in (None, account_id) and not re.fullmatch(r'p_[a-zA-Z0-9_-]+', value['account_id']):
            raise ValueError('身份已经绑定其他账户')
        value['account_id'] = account_id
        self.file(identity).write(value)

    @staticmethod
    def proof(value, challenge, purpose):
        if (challenge.get('origin') != value['platform'] or challenge.get('purpose') != purpose
                or challenge.get('role') != 'merchant' or challenge.get('public_key') != value['public_key']
                or not time.time() < challenge.get('expires', 0) <= time.time() + 360):
            raise ValueError('身份验证挑战无效')
        message = json.dumps(['teerouter-identity-v1', value['platform'], purpose, 'merchant',
                              value['public_key'], challenge['id'], challenge['nonce'], challenge['expires']],
                             separators=(',', ':'), ensure_ascii=False).encode()
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value['seed']))
        return {'challenge_id': challenge['id'], 'signature': key.sign(message).hex(), 'role': 'merchant'}
