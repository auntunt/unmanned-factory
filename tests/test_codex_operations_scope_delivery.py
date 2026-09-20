"""Independent route-level checks of scope-change delivery, with no paid model."""
import pytest

from factory.control import effective_contract as ec
from tests.test_control_app import app_env, login  # noqa: F401
from tests.test_effective_contract import _confirmed, _resumable, _analyst, _lifts_square


def test_followup_at_waiting_state_applies_the_new_scope(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    project, rid = _confirmed(client, store, repo, headers)
    _resumable(store, project, rid, repo)
    calls, submitted = [], []
    _analyst(svc, _lifts_square(), calls)
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: submitted.append(a))
    response = client.post(f'/api/v2/runs/{rid}/follow-up', headers=headers,
        json={'content': '我确认要加 square 功能', 'idempotency_key': 'waiting-square-1'})
    assert response.status_code == 200, response.text
    assert response.json()['applied'] is True
    assert ec.revision_of(store.get(rid)) == 2, 'An applied waiting-state supplement must reach the contract'
    assert len(calls) == 1


@pytest.mark.parametrize('outcome', ['unresolved', 'unavailable'])
def test_unresolved_or_failed_analysis_does_not_consume_and_dispatch(app_env, monkeypatch, outcome):
    client, store, svc, repo = app_env
    headers = login(client)
    project, rid = _confirmed(client, store, repo, headers)
    response = client.post(f'/api/v2/runs/{rid}/follow-up', headers=headers,
        json={'content': '增加 square，但业务范围请先确认', 'idempotency_key': 'unresolved-square-1'})
    assert response.status_code == 200
    _resumable(store, project, rid, repo)
    if outcome == 'unresolved':
        _analyst(svc, {'superseded_non_goals': [], 'added_requirements': [],
                       'unresolved': ['请确认业务范围']})
    else:
        def unavailable(*a, **kw):
            raise RuntimeError('test model unavailable')
        monkeypatch.setattr(svc.runner, 'run', unavailable)
    submitted = []
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: submitted.append(a))
    try:
        svc.continue_run(rid, '', 1, 0, 'owner')
    except (RuntimeError, ValueError):
        pass  # A clear refusal is also acceptable; durable state is authoritative.
    assert not list(store.export_events(rid, kind='followup.applied')), 'Unresolved input must remain pending'
    assert not submitted, 'Do not dispatch coding with undecided or unanalyzed scope'
    assert store.get(rid)['status'] == 'needs_human'
    assert ec.revision_of(store.get(rid)) == 1
