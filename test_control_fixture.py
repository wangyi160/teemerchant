"""In-memory platform/TEE split for existing account and browser fixtures."""
import copy
from credential_protocol import public_digest


def install(app, router):
    router.remote['merchant'].setdefault('runtime_id', 'fixture-runtime')
    router.remote['merchant']['control_protocol'] = 'platform-config-v1'
    original = router.call
    router._apply = original
    prepared = {}
    def tee(method, path, body=None):
        if path == '/merchant/accounts/prepare':
            assert body['public_digest']
            ticket = str(len(prepared)+1)
            prepared[ticket] = copy.deepcopy(body)
            return {'ticket':ticket, 'runtime_id':body['runtime_id']}
        assert path not in ('/merchant/state','/merchant/config','/merchant/models/refresh'), path
        return original(method, path, body)
    router.call = tee
    def fetch(path):
        assert path == 'state', path
        return copy.deepcopy(router.remote)
    def configure(body):
        public = copy.deepcopy(body['config'])
        assert 'accounts' not in public and 'secrets' not in public
        if body.get('ticket'):
            stage = prepared[body['ticket']]
            assert stage['public_digest'] == public_digest(public)
            material = stage['material']
            for key in ('accounts','secrets'):
                if material[key]: public[key] = copy.deepcopy(material[key])
            for pool in public['pools']:
                for d in pool['deployments']:
                    if d['id'] in material['proxies']:d['proxy']=material['proxies'][d['id']]
        for name, control in public.pop('account_controls', {}).items():
            public.setdefault('accounts', {}).setdefault(name, {}).update(control)
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
    app.platform_data.fetch = fetch
    app.platform_data.configure = configure
    return app
