import subprocess

import pytest

from factory.control.error_types import failure_type
from factory.control.execution import ExecutionError
from factory.control.inspections import InspectionStore, inspect_run
from factory.control.project_browser import BrowserUnavailable
from factory.control.providers import ProviderTimeout
from factory.control.store import Conflict
from tests.test_control_app import app_env
from tests.test_operation_workflows import schedule
from tests.test_operations_automation import setup, new_run


@pytest.mark.parametrize('exc, expected', [
    (BrowserUnavailable('opaque failure'), 'browser_unavailable'),
    (Conflict('opaque failure', error_type='budget'), 'budget'),
    (TimeoutError('opaque failure'), 'timeout'),
    (ProviderTimeout('opaque failure'), 'timeout'),
    (subprocess.TimeoutExpired('command', 1), 'timeout'),
    (ExecutionError('opaque failure', error_type='interrupted'), 'interrupted'),
])
def test_inspection_persists_original_failure_type(app_env, monkeypatch, exc, expected):
    service, store, project, config = schedule(app_env)
    rid = service.inspections.tick(config['next_at'])[0]
    def fail(*args):
        raise exc
    monkeypatch.setattr(service, '_independent_verify', fail)
    inspect_run(service, rid)
    run = store.get(rid)
    assert run['status'] == 'inspection_failed'
    assert run['error_type'] == expected
    assert run['error']


def test_inspection_automation_constructor(app_env):
    _, store, service, _ = app_env
    assert service.inspections.automation is service.operations_automation
    standalone = InspectionStore(store, service.operations)
    assert standalone.automation is None


def test_failure_type_survives_wrapping():
    try:
        raise Conflict('opaque', error_type='budget')
    except Conflict as exc:
        wrapped = ExecutionError('rewritten', artifacts={'verification': {'error_type': 'unverified'}})
        wrapped.__cause__ = exc
    assert failure_type(wrapped) == 'budget'
    assert failure_type(ExecutionError('opaque', artifacts={
        'verification': {'error_type': 'browser_unavailable'}})) == 'browser_unavailable'


@pytest.mark.parametrize('error_type, reason, expected', [
    ('timeout', '预算', 'timeout'),
    ('browser_unavailable', 'timeout', 'environment'),
    ('budget', 'Chrome unavailable', 'budget'),
    ('new_failure', 'budget', 'health_check'),
    (None, 'budget exhausted', 'budget'),
])
def test_history_prefers_structured_type_and_keeps_legacy_fallback(app_env, error_type, reason, expected):
    _, store, automation, project, _ = setup(app_env)
    run = new_run(store, project['id'], inspection=True)
    store.update(run['id'], {'status': 'inspection_failed', 'error': reason, 'error_type': error_type},
                 event=('inspection.failed', {'message': reason}))
    automation.tick()
    history = automation.history(project['id'])['history']
    assert history[0]['failure_category'] == expected
    # Later structured metadata must not rewrite existing historical conclusions.
    store.update(run['id'], {'error_type': 'interrupted'})
    automation.tick()
    assert automation.history(project['id'])['history'] == history


def test_budget_gate_marks_conflict_at_source():
    from types import SimpleNamespace
    from factory.control.run_billing import _remaining_dollar_budget
    service = SimpleNamespace(
        _dollar_budget=lambda *args: SimpleNamespace(exhausted=True, limit_usd=1),
        _budget_usage=lambda *args: {'unknown_cost_reserved_usd': 0, 'known_cost_usd': 1})
    with pytest.raises(Conflict) as caught:
        _remaining_dollar_budget(service, 'run', {})
    assert caught.value.error_type == 'budget'
