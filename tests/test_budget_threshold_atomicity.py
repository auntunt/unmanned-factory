"""Two concurrent billing checks must still produce exactly one reminder.

The independent probe synchronises inside `run_billing.all_events`, which is now
only a cheap early exit: the decision moved into the serialized transaction of
`Store.append_once`, so lining up that read no longer lines up the critical
section. That probe is kept verbatim in tests/test_codex_budget_candidate_review
.py; these tests re-arm the schedule at the boundary that now decides.

The schedule here pairs the callers *between* the read and the write: each one
announces it has read and waits for the other. Without the lock both reads
complete before either write, so both append and the count is 2. With the lock
the second caller cannot even start its read, so the first waits out a short
timeout, commits, and the second then sees the duplicate -- the wait timing out
is the locked outcome, not a flake. Both directions are deterministic.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.test_control_app import app_env, login, project


def _warnings(store, rid):
    return [e for e in store.events(rid) if e['type'] == 'budget.threshold_warning']


def _pair_between_read_and_write(store, expected=2, timeout=1.5):
    """Hold each writer until every caller has read, or until the lock proves it can't."""
    original = type(store)._event
    guard, arrived, ready = threading.Lock(), [], threading.Event()

    def hooked(db, rid, kind, payload, task_id=None):
        with guard:
            arrived.append(kind)
            if len(arrived) >= expected:
                ready.set()
        ready.wait(timeout)
        return original(db, rid, kind, payload, task_id)

    return staticmethod(hooked)


def _concurrently(service, rid, budget, workers=2):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(service._warn_budget_threshold, rid, budget) for _ in range(workers)]
        for job in jobs:
            job.result(timeout=30)


def test_concurrent_checks_at_the_write_boundary_emit_one_reminder(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))  # budget_usd == 10
    run, _ = store.create_run(p['id'], '并发结账只提醒一次')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    budget = service._dollar_budget(run['id'], p)
    monkeypatch.setattr(type(store), '_event', _pair_between_read_and_write(store))
    _concurrently(service, run['id'], budget)
    assert len(_warnings(store, run['id'])) == 1


def test_a_raised_ceiling_still_earns_its_own_reminder_under_contention(app_env, monkeypatch):
    """The dedup key is (run, effective ceiling), not "warned once ever"."""
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '续额后并发结账')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    service._warn_budget_threshold(run['id'], service._dollar_budget(run['id'], p))
    assert [e['payload']['limit_usd'] for e in _warnings(store, run['id'])] == [10]
    store.update(run['id'], {'budget_credit_usd': 10})  # admin granted more
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.0})
    raised = service._dollar_budget(run['id'], p)
    assert raised.limit_usd == 20
    monkeypatch.setattr(type(store), '_event', _pair_between_read_and_write(store))
    _concurrently(service, run['id'], raised)
    assert [e['payload']['limit_usd'] for e in _warnings(store, run['id'])] == [10, 20]


def test_append_once_is_the_boundary_not_the_caller(app_env):
    """A duplicate is refused even when the caller skipped its own pre-check."""
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '直接调用也去重')
    first = store.append_once(run['id'], 'budget.threshold_warning', {'limit_usd': 10.0},
                              duplicate=lambda payload: payload.get('limit_usd') == 10.0)
    second = store.append_once(run['id'], 'budget.threshold_warning', {'limit_usd': 10.0},
                               duplicate=lambda payload: payload.get('limit_usd') == 10.0)
    other = store.append_once(run['id'], 'budget.threshold_warning', {'limit_usd': 20.0},
                              duplicate=lambda payload: payload.get('limit_usd') == 20.0)
    assert first and second is None and other
    assert [e['payload']['limit_usd'] for e in _warnings(store, run['id'])] == [10.0, 20.0]
