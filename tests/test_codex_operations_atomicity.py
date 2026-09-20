"""Independent persistence fault probe: an unapplied note must not resume a run.

No paid model, background execution, or production data. Inject failure at the
durable applied receipt; the contract/state change must share its transaction.
"""
import subprocess

import pytest

from tests.test_control_app import app_env, login, project  # noqa: F401


def test_failed_applied_receipt_rolls_back_consumption_and_resume(app_env, monkeypatch):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    run, _ = store.create_run(p['id'], 'Update greeting, preserve its format',
                              source={'type': 'web', 'operation': 'bugfix'})
    rid = run['id']
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    store.update(rid, {
        'status': 'running', 'revision': 1,
        'plan': {'title': 'Greeting', 'summary': '', 'questions': [], 'tasks': [{
            'id': 'greeting', 'title': 'Greeting', 'prompt': 'Update greeting.txt',
            'acceptance': ['greeting is hello world'], 'paths': ['greeting.txt'],
            'checks': ['greeting'], 'depends_on': [], 'complexity': 'small', 'risk': 'low'}]},
        'artifacts': {'base_sha': base, 'tasks': [{'id': 'greeting'}]},
        'execution_checks': store.project(p['id'])['checks'],
    })
    response = client.post(f'/api/v2/runs/{rid}/follow-up', headers=headers,
                           json={'content': '保留现有格式，继续完成原任务',
                                 'idempotency_key': 'codex-atomic-receipt-1'})
    assert response.status_code == 200, response.text
    store.update(rid, {'status': 'needs_human'})
    before = store.get(rid)
    actual_event = store._event
    injected = []
    submissions = []

    def fail_applied(db, run_id, kind, payload, task_id=None):
        if run_id == rid and kind == 'followup.applied':
            injected.append(payload)
            raise RuntimeError('codex simulated storage failure before applied receipt')
        return actual_event(db, run_id, kind, payload, task_id)

    monkeypatch.setattr(store, '_event', fail_applied)
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: submissions.append((a, kw)))
    with pytest.raises(RuntimeError, match='codex simulated storage failure'):
        svc.continue_run(rid, '', 1, 0, 'owner')
    assert injected, 'Fault must hit the actual applied persistence boundary'
    after = store.get(rid)
    assert after['status'] == before['status'], 'A failed receipt cannot leave execution queued'
    assert after['history'] == before['history'], 'Failed consumption cannot persist merged history'
    assert after.get('resume_count', 0) == before.get('resume_count', 0)
    assert after.get('execution_resume') == before.get('execution_resume')
    assert not list(store.export_events(rid, kind='followup.applied'))
    assert not submissions, 'No execution should be dispatched after failed durable application'
