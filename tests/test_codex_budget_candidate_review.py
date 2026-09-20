"""Independent acceptance probes for candidate 6189591; no paid model calls."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from factory.control.execution import ExecutionError
from factory.control.providers import ProviderResult
from tests.review_helpers import passing_review
from tests.test_control_app import app_env, login, project
from tests.test_verification_format_repair import _review


@pytest.mark.parametrize('report_kind', ['negated_pass', 'missing_evidence', 'multiple_json'])
def test_format_repair_cannot_invent_success(app_env, report_kind):
    _, store, service, repo, p, run, cfg = _review(app_env)
    calls = []
    repaired = None

    def respond(request, emit, cancel=None):
        nonlocal repaired
        calls.append(request)
        if len(calls) == 1:
            repaired = passing_review(request, 'INVENTED evidence absent from original report')
            if report_kind == 'negated_pass':
                original = 'I cannot pass this change. Required evidence is unavailable.'
            elif report_kind == 'missing_evidence':
                original = 'Overall: pass. No per-criterion evidence has been collected.'
            else:
                a = json.dumps({'verdict': 'pass', 'reason': 'first report'})
                b = json.dumps({'verdict': 'pass', 'reason': 'second report'})
                original = f'```json\n{a}\n```\n```json\n{b}\n```'
            return ProviderResult(original, cost_usd=0.03)
        return ProviderResult(repaired, cost_usd=0.01)

    service.runner.run = respond
    artifacts = {'worktree': str(repo)}
    with pytest.raises(ExecutionError):
        service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert artifacts.get('verification', {}).get('verdict') != 'pass'


def test_threshold_reminder_reaches_live_conversation_api(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'show the nonblocking reminder')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    service._remaining_dollar_budget(run['id'], p)
    warnings = [e for e in store.events(run['id']) if e['type'] == 'budget.threshold_warning']
    assert len(warnings) == 1
    response = client.get(f"/api/v2/runs/{run['id']}/conversation")
    assert response.status_code == 200
    assert any(warnings[0]['payload']['message'] in m['content'] for m in response.json()['messages'])


def test_parallel_budget_checks_emit_only_one_reminder(app_env, monkeypatch):
    from factory.control import run_billing
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], 'parallel billing dispatch')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    budget = service._dollar_budget(run['id'], p)
    original = run_billing.all_events
    barrier = threading.Barrier(2)

    def concurrent_read(*args, **kwargs):
        events = list(original(*args, **kwargs))
        barrier.wait(timeout=5)
        return events

    monkeypatch.setattr(run_billing, 'all_events', concurrent_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(service._warn_budget_threshold, run['id'], budget) for _ in range(2)]
        for job in jobs:
            job.result(timeout=10)
    assert len([e for e in store.events(run['id']) if e['type'] == 'budget.threshold_warning']) == 1
