"""Windows DPAPI compatibility; Linux AES-GCM with an owner-only local key."""
import os
import stat
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

class LinuxVault:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / '.vault-key'
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as file:
                file.write(os.urandom(32)); file.flush(); os.fsync(file.fileno())
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise RuntimeError('商户密钥文件须归当前服务用户所有且权限为 0600')
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError('商户本地密钥损坏，请恢复 data 备份')
        self.cipher = AESGCM(key)

    def __call__(self, value, decrypt=False):
        if decrypt:
            if not value.startswith(b'TMV1'):
                raise RuntimeError('此账号库属于 Windows DPAPI；须先在原 Windows 系统导出再迁移')
            try:
                return self.cipher.decrypt(value[4:16], value[16:], b'teemerchant-v1')
            except InvalidTag:
                raise ValueError('商户资料无法解密，请恢复与账号库匹配的本地密钥') from None
        nonce = os.urandom(12)
        return b'TMV1' + nonce + self.cipher.encrypt(nonce, value, b'teemerchant-v1')
