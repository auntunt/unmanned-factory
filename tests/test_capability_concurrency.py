from concurrent.futures import ThreadPoolExecutor
import threading

from factory.control.capabilities import CapabilityStore, TEMPLATES
from factory.control.store import Store


def test_parallel_capability_bootstrap_is_idempotent(tmp_path, monkeypatch):
    store = Store(tmp_path / 'control.db')
    barrier = threading.Barrier(8)
    original = CapabilityStore._seed

    def together(registry, db):
        barrier.wait(timeout=10)
        original(registry, db)

    monkeypatch.setattr(CapabilityStore, '_seed', together)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: CapabilityStore(store).list(), range(8)))
    assert all(len(result) == len(TEMPLATES) for result in results)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM capability_audit WHERE type='capability.seeded'").fetchone()[0] == len(TEMPLATES)
