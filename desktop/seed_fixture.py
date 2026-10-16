"""Isolated desktop test data. Never contacts a real Router."""
import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application
from store import Store
from test_browser import FakeRouter
from test_revenue import receipt

store = Store(sys.argv[1])
try:
    store.settings['router'] = 'https://fixture.invalid'
    store.save()
    router = FakeRouter(store)
    Application(store, router).accept(copy.deepcopy(router.remote))
    store.archive('usage', 'fixture-boot', [receipt('desktop-revenue')])
finally:
    store.db.close()
