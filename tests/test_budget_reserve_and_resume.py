"""One complete task: the review reserve holds, and resuming does not reset it."""
import pytest

from factory.control import budget_resume
from factory.control.autonomy import all_events
from factory.control.run_billing import _verification_reserve_usd
from tests.test_continuous_service import prepared
from tests.test_workbench_app import app_env


def _stages(store, rid):
    return [e['payload'] for e in all_events(store, rid)
            if e['type'] == 'budget.stage_allocated']


def test_the_review_reserve_is_withheld_from_the_coding_call(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {'worktree': p['workspace'], 'tasks': [], 'checks': [],
                'commit': store.get(rid)['context']['commit_sha'], 'known_cost_usd': 0}

    svc.continuous_execute = executor
    monkeypatch.setattr(svc, '_independent_verify', lambda *args: None)
    svc._run(rid)
    reserve = _verification_reserve_usd(10.0)
    assert reserve == pytest.approx(2.0)
    # The coding stage is capped below the run budget; review keeps the rest.
    assert calls[0]['project']['budget_usd'] == pytest.approx(10.0 - reserve)
    assert calls[0]['project']['verification_budget_reserved_usd'] == pytest.approx(reserve)
    stage = _stages(store, rid)[0]
    assert stage == {'stage': 'execution', 'remaining_usd': pytest.approx(10.0),
                     'max_budget_usd': pytest.approx(8.0),
                     'verification_reserved_usd': pytest.approx(2.0)}


def test_development_cannot_eat_the_reserve_and_the_stop_is_explicit(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch, budget_usd=2.0)
    svc._plan(rid)
    # Coding already spent everything outside the reserve; the whole remainder
    # belongs to independent review, so a new coding call must be refused.
    store.append(rid, 'usage.recorded', {'profile': 'standard', 'cost_usd': 1.0})
    store.update(rid, {'status': 'needs_human', 'artifacts': {
        'execution_mode': 'continuous', 'worktree': p['workspace'],
        'base_sha': store.get(rid)['context']['commit_sha'],
        'branch': p['base_branch'], 'commit': None,
        'verification_budget_reserved_usd': 1.0,
        'checks': [{'name': 'behavior', 'argv': ['pytest'], 'exit': 1,
                    'timeout': False, 'cancelled': False, 'stdout': '', 'stderr': 'boom'}],
        'tasks': [{'id': 'coding', 'status': 'failed', 'attempts': [{
            'status': 'failed', 'command_evidence': [{
                'command': 'pytest', 'exit_code': 0, 'output': 'tests ran'}]}]}]}})
    svc.continuous_execute = lambda **kwargs: pytest.fail(
        'the reserve must not be spent on another coding call')
    current = store.get(rid)
    svc.continue_run(rid, '', current['revision'], current.get('resume_count', 0), 'owner')
    svc._run(rid)
    stopped = store.get(rid)
    assert stopped['status'] == 'needs_human'
    assert '独立验收' in stopped['artifacts']['needs_human']
    assert stopped['artifacts']['budget_exhausted'] is True


def test_resume_adds_credit_without_resetting_the_ledger_or_double_reserving(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    store.append(rid, 'usage.recorded', {'profile': 'standard', 'cost_usd': 9.5})
    store.update(rid, {'status': 'needs_human', 'artifacts': {
        'execution_mode': 'continuous', 'worktree': p['workspace'],
        'base_sha': store.get(rid)['context']['commit_sha'],
        'branch': p['base_branch'], 'commit': None,
        'budget_exhausted': True, 'needs_human': '预算已用尽',
        'verification_budget_reserved_usd': 2.0,
        'tasks': [{'id': 'coding', 'status': 'failed', 'attempts': []}]}})
    calls = []

    def executor(**kwargs):
        calls.append(kwargs)
        return {'worktree': p['workspace'], 'tasks': [], 'checks': [],
                'commit': store.get(rid)['context']['commit_sha'], 'known_cost_usd': 0}

    svc.continuous_execute = executor
    monkeypatch.setattr(svc, '_independent_verify', lambda *args: None)
    current = store.get(rid)
    budget_resume.resume(svc, rid, current['revision'], current.get('resume_count', 0), 'owner')
    svc._run(rid)
    resumed = store.get(rid)
    # One credit of one project budget, and the spend so far is still counted.
    assert resumed['budget_credit_usd'] == pytest.approx(10.0)
    assert svc._usage(rid)['known_cost_usd'] == pytest.approx(9.5)
    renewals = [e['payload'] for e in all_events(store, rid) if e['type'] == 'budget.renewed']
    assert len(renewals) == 1 and renewals[0]['additional_usd'] == pytest.approx(10.0)
    # Remaining is 20 - 9.5, and exactly one reserve is taken out of it.
    reserve = _verification_reserve_usd(10.5)
    assert calls[0]['project']['verification_budget_reserved_usd'] == pytest.approx(reserve)
    assert calls[0]['project']['budget_usd'] == pytest.approx(10.5 - reserve)
    assert len(_stages(store, rid)) == 1


def test_time_alone_adds_no_money(app_env, monkeypatch):
    store, svc, p, rid, _ = prepared(app_env, monkeypatch)
    svc._plan(rid)
    store.append(rid, 'usage.recorded', {'profile': 'standard', 'cost_usd': 4.0})
    first = svc._remaining_dollar_budget(rid, store.project(p['id']))
    store.update(rid, {'status': 'running'})  # a long task simply keeps running
    later = svc._remaining_dollar_budget(rid, store.project(p['id']))
    assert later.limit_usd == first.limit_usd == pytest.approx(10.0)
    assert later.remaining_usd == first.remaining_usd == pytest.approx(6.0)
