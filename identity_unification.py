"""One-time, restartable migration of local vault ownership to public-key IDs.

Encrypted backups are retained in the portable data directory. Provider tokens,
owner secrets and signed historical records are never regenerated or rewritten.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


def remap(value, mapping):
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        for old, new in mapping.items():
            if value.startswith((old + '.', old + '/')):
                return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key, item = remap(key, mapping), remap(item, mapping)
            if key in result and result[key] != item:
                raise ValueError('Identity migration collision')
            result[key] = item
        return result
    return value


def migrate(directory, protector, extra_mapping=None):
    root = Path(directory).resolve()
    if not root.exists():
        return
    mapping, profiles = dict(extra_mapping or {}), []
    paths = [root / 'settings.dpapi', *sorted((root / 'merchants').glob('m_*/settings.dpapi'))]
    for path in paths:
        if not path.is_file():
            continue
        value = json.loads(protector(path.parent)(path.read_bytes(), True))
        account = value.get('platform_identity', {})
        identity = value.get('merchant_id') or account.get('identity_id')
        if not account.get('identity_id') or not identity or not re.fullmatch(r'm_sig_[a-f0-9]{64}', identity):
            continue
        # Possession and integrity of the pre-existing identity key is required.
        from key_identities import Identities
        local = Identities(root).load(account.get('platform_url', value.get('platform_url')), identity)
        old = 'm_' + hashlib.sha256(value['token'].encode()).hexdigest()[:32]
        mapping[old] = identity
        if account.get('account_id') and account['account_id'] != identity:
            mapping[account['account_id']] = identity
        if local.get('account_id') and local['account_id'] != identity:
            mapping[local['account_id']] = identity
        profiles.append((path, identity))
    if not profiles:
        return

    backup = root / 'identity-migration-backup'
    def rewrite(path, value, encrypted=True):
        protect = protector(path.parent)
        raw = path.read_bytes() if path.exists() else None
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        if raw is not None:
            previous = protect(raw, True) if encrypted else raw
            if json.loads(previous) == value:
                return
            saved = backup / path.relative_to(root)
            if not saved.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(raw)
                key = path.parent / '.vault-key'
                if key.exists():
                    shutil.copyfile(key, saved.parent / '.vault-key')
                    os.chmod(saved.parent / '.vault-key', 0o600)
        temporary = path.with_suffix(path.suffix + '.identity-tmp')
        with temporary.open('wb') as file:
            file.write(protect(encoded) if encrypted else encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)

    for path, identity in profiles:
        value = remap(json.loads(protector(path.parent)(path.read_bytes(), True)), mapping)
        value['merchant_id'] = identity
        value.pop('router_access', None)  # Reissue the JWT with its canonical subject.
        for name in ('tdx_policy', 'snp_policy'):
            policy = Path(value.get(name) or '')
            if policy.is_absolute() and policy.is_relative_to(path.parent):
                value[name] = policy.relative_to(path.parent).as_posix()
        rewrite(path, value)
        book = path.parent / 'accounts.dpapi'
        if book.exists():
            value = remap(json.loads(protector(book.parent)(book.read_bytes(), True)), mapping)
            value['identity'] = identity
            rewrite(book, value)
        if path.parent != root and path.parent.name != identity:
            target = (root / 'merchants' / identity).resolve()
            if not target.is_relative_to(root) or target.exists():
                raise ValueError('Identity migration destination conflict')
            os.replace(path.parent, target)
    for path in [root / 'platform-sessions.dpapi', *sorted((root / 'identities').glob('m_sig_*.dpapi'))]:
        if path.exists():
            rewrite(path, remap(json.loads(protector(path.parent)(path.read_bytes(), True)), mapping))
    pointer = root / 'active-merchant.json'
    if pointer.exists():
        rewrite(pointer, remap(json.loads(pointer.read_text(encoding='utf-8')), mapping), False)
