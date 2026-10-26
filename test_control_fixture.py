"""In-memory platform/TEE split for existing account and browser fixtures."""
import copy
from credential_protocol import public_digest


def install(app, router):
    router.remote['merchant'].setdefault('runtime_id', 'fixture-runtime')
    router.remote['merchant']['control_protocol'] = 'platform-config-v1'
    original = router.call
    router._apply = original
    def tee(method, path, body=None):
        if path == '/merchant/accounts/sync':
            from local_accounts import config_from
            update = config_from(router.remote)
            existing = {p['name']:p for p in router.remote['pools']}
            update['pools'] = [{**copy.deepcopy(existing.get(p['name'],p)), 'deployments':copy.deepcopy(p['deployments'])} for p in body['pools']]
            names = {p['name'] for p in body['pools'] if p['deployments']}
            update['pool_pricing'] = [p for p in update['pool_pricing'] if p['pool'] in names] + copy.deepcopy(body['new_pool_pricing'])
            for k in ('expected_revision','accounts','secrets','delete_accounts','delete_secrets'):update[k]=copy.deepcopy(body[k])
            return original('PUT','/fixture/config',update)
        assert path not in ('/merchant/state','/merchant/config','/merchant/models/refresh','/merchant/accounts/prepare'), path
        return original(method, path, body)
    router.call = tee
    def fetch(path):
        assert path == 'state', path
        return copy.deepcopy(router.remote)
    def configure(body):
        public = copy.deepcopy(body['config'])
        assert 'accounts' not in public and 'secrets' not in public
        pending = app.store.accounts.pending(app.store.settings['router'])
        if pending and body['config'].get('delete_accounts') is not None:
            for name in ('accounts','secrets'):
                if name in pending:public.setdefault(name,{})
        if public['expected_revision'] != router.remote['settings']['revision']:
            raise RuntimeError('Router 返回 409：revision_conflict')
        try:
            return router._apply('PUT', '/fixture/config', public)
        except Exception:
            result = router._apply('GET', '/fixture/state')
            if result['settings']['revision'] > public['expected_revision']:
                return result
            raise
    app.platform_data.notify_synced = lambda value: {'accepted':True}
    app.platform_data.fetch = fetch
    app.platform_data.configure = configure
    return app
