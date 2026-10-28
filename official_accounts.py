"""Only the three official providers can be stored or synchronized."""
import copy

PROVIDERS = frozenset(('openai', 'anthropic', 'xai'))


def retire_unsupported(data):
    """Remove retired pools and their private material; preserve shared official refs."""
    value = copy.deepcopy(data)
    for book in value.get('routers', {}).values():
        configs = [v for v in (book.get('config'), book.get('pending'), book.get('remote')) if isinstance(v, dict)]
        removed_pools, removed_accounts, removed_secrets, removed_deployments = set(), set(), set(), set()
        kept_accounts, kept_secrets = set(), set()
        for config in configs:
            for pool in config.get('pools', []):
                deployments = pool.get('deployments', [])
                provider = pool.get('provider') or (deployments[0].get('provider') if deployments else None)
                retired = provider not in PROVIDERS or any(d.get('provider') not in PROVIDERS for d in deployments)
                if retired:
                    removed_pools.add(pool['name'])
                    removed_deployments.update(d['id'] for d in deployments)
                for deployment in deployments:
                    account = deployment.get('oauth_account')
                    secret = deployment.get('api_key_ref') or deployment.get('api_key_env')
                    if account: (removed_accounts if retired else kept_accounts).add(account)
                    if secret: (removed_secrets if retired else kept_secrets).add(secret)
        removed_accounts -= kept_accounts
        removed_secrets -= kept_secrets
        removed_accounts.update(k for k, a in book.get('accounts', {}).items()
                                if a.get('provider') is not None and a['provider'] not in PROVIDERS)
        for field, removed in [('account_sync', removed_deployments), ('sync_errors', removed_deployments),
                               ('token_sync', removed_accounts), ('renewals', removed_accounts)]:
            if isinstance(book.get(field), dict):
                book[field] = {k: v for k, v in book[field].items() if k not in removed}
        for config in configs:
            config['pools'] = [p for p in config.get('pools', []) if p['name'] not in removed_pools]
            for field in ('pool_pricing', 'prices'):
                if field in config: config[field] = [p for p in config[field] if p.get('pool') not in removed_pools]
            if isinstance(config.get('compatibility'), dict):
                config['compatibility'] = {p: v for p, v in config['compatibility'].items() if p in PROVIDERS}
            if isinstance(config.get('merchant'), dict): config['merchant'].pop('allow_test', None)
            for field in ('settings',):
                settings = config.get(field)
                if isinstance(settings, dict):
                    for name in ('pool_pricing', 'prices'):
                        if name in settings: settings[name] = [p for p in settings[name] if p.get('pool') not in removed_pools]
            if isinstance(config.get('catalog'), dict):
                config['catalog']['data'] = [r for r in config['catalog'].get('data', []) if r.get('pool') not in removed_pools]
            if isinstance(config.get('status'), dict) and 'models' in config['status']:
                config['status']['models'] = [p for p in config['status']['models'] if p.get('model') not in removed_pools]
            if isinstance(config.get('official_pricing'), dict):
                config['official_pricing']['providers'] = [p for p in config['official_pricing'].get('providers', []) if p.get('provider') in PROVIDERS]
        for config in [book, *configs]:
            accounts = config.get('accounts')
            if isinstance(accounts, dict):
                if isinstance(accounts.get('data'), list):
                    accounts['data'] = [a for a in accounts['data'] if (a.get('provider') is None or a['provider'] in PROVIDERS) and a.get('name') not in removed_accounts]
                else:
                    config['accounts'] = {k: a for k, a in accounts.items() if k not in removed_accounts and a.get('provider', 'openai') in PROVIDERS}
            if isinstance(config.get('secrets'), dict):
                config['secrets'] = {k: v for k, v in config['secrets'].items() if k not in removed_secrets}
            for field, removed in [('delete_accounts', removed_accounts), ('delete_secrets', removed_secrets)]:
                if field in config: config[field] = [k for k in config[field] if k not in removed]
        if removed_pools or removed_accounts or removed_secrets:
            book['local_revision'] = book.get('local_revision', 0) + 1
    return value
