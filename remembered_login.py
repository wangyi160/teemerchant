"""Portable encrypted storage for login sessions."""
import json
import os
from pathlib import Path
import tempfile

from store import protector

LIFETIME = 30 * 24 * 3600
RENEW_INTERVAL = 24 * 3600
BROWSER_LIFETIME = 365 * 24 * 3600


class SessionFile:
    def __init__(self, directory, name):
        self.path = Path(directory) / name
        self.protect = protector(directory)

    def read(self):
        try:
            if self.path.stat().st_size > 1024 * 1024:
                return {}
            value = json.loads(self.protect(self.path.read_bytes(), True))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, RuntimeError):
            # An unreadable session never authorizes access to a saved vault.
            return {}

    def write(self, value):
        data = self.protect(json.dumps(value, ensure_ascii=False).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.login-')
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
