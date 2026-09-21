"""Answering a model's question, on all three business surfaces.

One contract, three surfaces: when a run stops at ``needs_clarification`` the
task view shows the questions, and ``POST .../clarify`` carries the answer to
the run lifecycle. Answering is deliberately not resuming -- 「继续执行」 on a
run stopped at a question only pushes it back at the same gate.

The happy path (answer -> re-plan -> approve) is exercised end to end against a
real service in the scripted rehearsal, because it needs a planner that asks a
question. What is asserted here is everything that must refuse, plus the wiring:
that the route really hands the answer to ``svc.clarify``.
"""
from __future__ import annotations

import subprocess
import uuid

from factory.control.issue_maintenance import (
    MaintenanceStore, content_fingerprint, normalize,
)
from factory.control.issue_maintenance_webuddy import SOURCE_TYPE
from factory.control.plugins import PluginAvailability
from tests.test_control_app import app_env, login, project  # noqa: F401

QUESTIONS = ['金额是按含税还是不含税口径？', '旧接口要保留多久？']


def _base_sha(repo):
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo,
                                   text=True).strip()


def _task_awaiting_clarification(client, store, repo, headers):
    """A maintenance task whose run stopped on two questions."""
    p = project(client, repo, headers)
    user = client.get('/api/auth/me', headers=headers).json()['user']
    task_id = uuid.uuid4().hex
    key = f'clarify-{uuid.uuid4().hex[:12]}'
    run, _ = store.create_run(
        p['id'], 'maintenance clarification test',
        source={'type': SOURCE_TYPE, 'actor': user['username'],
                'actor_id': user['id'], 'maintenance_task_id': task_id,
                'repository': p['repository']},
        delivery_id=f'maintenance:{task_id}')
    store.update(run['id'], {'status': 'needs_clarification',
                             'plan': {'title': '待澄清', 'questions': QUESTIONS,
                                      'tasks': []}})
    records = MaintenanceStore(store)
    normalized = normalize({
        'issue': {'source': 'test', 'external_id': '1', 'version': '1',
                  'title': '报表金额不对', 'body': '跨月导出金额对不上'},
        'project_id': p['id'], 'repository': p['repository'],
        'base_sha': _base_sha(repo), 'expected_behaviour': '金额一致',
        'delivery_goal': '补丁', 'agreement': {'revision': '1'},
        'idempotency_key': key})
    record, _ = records.claim(f'maintenance:{p["id"]}:{key}', normalized,
                              content_fingerprint(normalized))
    records.link(record['id'], {'execution_id': run['id']})
    return record['id'], run['id'], p


def test_the_questions_are_visible_and_named_as_the_reason_it_stopped(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, run_id, _ = _task_awaiting_clarification(client, store, repo, headers)

    view = client.get(f'/api/v2/maintenance/tasks/{task_id}', headers=headers)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body['pending_questions'] == QUESTIONS
    assert body['blocking_reason']['kind'] == 'clarification.requested'
    # Answering is not resuming, and the view must not offer the wrong verb.
    assert body['resumable'] is False


def test_an_empty_answer_is_refused(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, run_id, _ = _task_awaiting_clarification(client, store, repo, headers)
    for answer in ('', '   ', None, 42):
        refused = client.post(f'/api/v2/maintenance/tasks/{task_id}/clarify',
                              json={'answer': answer}, headers=headers)
        assert refused.status_code == 422, (answer, refused.text)
    assert store.get(run_id)['status'] == 'needs_clarification'


def test_a_stopped_plugin_does_not_accept_an_answer(app_env):
    """Answering starts the executor, so the continue gate applies unchanged."""
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, run_id, _ = _task_awaiting_clarification(client, store, repo, headers)
    availability = PluginAvailability(store)
    availability.set_state('issue-maintenance', 'draining', actor='owner')
    with store.connect() as db:
        # Straight to disabled: the drain check would refuse while this run is
        # live, and what is under test is the gate on answering, not the stop.
        db.execute("UPDATE plugin_availability SET state='disabled' WHERE plugin_id=?",
                   ('issue-maintenance',))

    refused = client.post(f'/api/v2/maintenance/tasks/{task_id}/clarify',
                          json={'answer': '按不含税口径'}, headers=headers)
    assert refused.status_code == 409, refused.text
    assert '已停用' in refused.json()['detail']
    assert store.get(run_id)['status'] == 'needs_clarification'


def test_a_member_without_the_project_cannot_read_or_answer(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, run_id, _ = _task_awaiting_clarification(client, store, repo, headers)

    client.app.state.auth.create_user('outsider', 'member-long-password',
                                      role='member')
    response = client.post('/api/auth/login',
                           json={'username': 'outsider',
                                 'password': 'member-long-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200
    outsider = {'Origin': 'http://testserver',
                'X-CSRF-Token': response.json()['csrf_token']}

    assert client.get(f'/api/v2/maintenance/tasks/{task_id}',
                      headers=outsider).status_code in (403, 404)
    blocked = client.post(f'/api/v2/maintenance/tasks/{task_id}/clarify',
                          json={'answer': '随便答一个'}, headers=outsider)
    assert blocked.status_code in (403, 404), blocked.text
    assert store.get(run_id)['status'] == 'needs_clarification'


def test_the_answer_really_reaches_the_run_lifecycle(app_env, monkeypatch):
    """The wiring, without dispatching a planner: ``svc.clarify`` is called with
    this run, this answer and this actor."""
    client, store, svc, repo = app_env
    headers = login(client)
    task_id, run_id, _ = _task_awaiting_clarification(client, store, repo, headers)
    seen = {}

    def fake_clarify(rid, answer, actor, **kwargs):
        seen.update(rid=rid, answer=answer, actor=actor)
        store.update(rid, {'status': 'planning', 'plan': None})
        return store.get(rid)

    monkeypatch.setattr(svc, 'clarify', fake_clarify)
    answered = client.post(f'/api/v2/maintenance/tasks/{task_id}/clarify',
                           json={'answer': '  按不含税口径，旧接口保留两个版本  '},
                           headers=headers)
    assert answered.status_code == 200, answered.text
    assert seen['rid'] == run_id
    # Trimmed, not passed through raw.
    assert seen['answer'] == '按不含税口径，旧接口保留两个版本'
    assert seen['actor'] == 'owner'
    # And the caller reads back the real state rather than an optimistic one.
    assert answered.json()['pending_questions'] == []


def test_all_three_surfaces_expose_the_same_answer_endpoint():
    """One contract, three surfaces -- so the page can reuse one component."""
    from factory.control import (adaptation_routes, maintenance_routes,
                                 modernization_routes)
    for module, suffix in ((maintenance_routes, '/tasks/{task_id}/clarify'),
                           (adaptation_routes, '/tasks/{task_id}/clarify'),
                           (modernization_routes, '/slices/{slice_id}/clarify')):
        source = open(module.__file__, encoding='utf-8').read()
        assert f"@api.post('{suffix}')" in source, module.__name__
        assert "gate.require('continue')" in source, module.__name__
        assert '_pending_questions' in source, module.__name__
