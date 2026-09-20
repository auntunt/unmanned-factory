# Legacy execution assertions explicitly use maintenance; default general confirmation is covered in test_requirement_analysis.py.
"""Monitoring is a project setting, never a hidden finite provider ceiling."""
import threading
from dataclasses import replace

import pytest

from factory.control.app import Project, ProjectUpdate, NewWorkspace, ConnectProject
from factory.control.providers import ProviderResult
from factory.control.store import Conflict
from tests.test_control_app import app_env, login, project, wait_state
from tests.review_helpers import passing_review


def test_new_project_models_default_to_monitoring():
    assert Project(name='Example', repository='owner/repo', workspace='/tmp/repo').budget_usd is None
    # Ordinary creation paths no longer carry a dollar field at all.
    assert 'budget_usd' not in NewWorkspace.model_fields
    assert 'budget_usd' not in ConnectProject.model_fields
    with pytest.raises(ValueError):
        NewWorkspace(name='Example', idempotency_key='abcdefgh', budget_usd=10)
    with pytest.raises(ValueError):
        ConnectProject(candidate_id='a' * 64, budget_usd=10)
    for invalid in (-1, 0, float('inf'), float('nan')):
        with pytest.raises(ValueError):
            ProjectUpdate(name='Example', base_branch='main', revision=1, budget_usd=invalid)
    assert ProjectUpdate(name='Example', base_branch='main', revision=1, budget_usd=10000).budget_usd == 10000


def test_budget_can_be_disabled_with_audited_cas_and_omission_preserves_it(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    url = f"/api/v2/projects/{p['id']}"
    body = {key: p[key] for key in ('name', 'base_branch', 'checks', 'revision')}
    unchanged = client.put(url, json=body, headers=headers)
    assert unchanged.status_code == 200
    assert unchanged.json()['budget_usd'] == 10
    body.update(revision=unchanged.json()['revision'], budget_usd=None)
    response = client.put(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['budget_usd'] is None
    assert client.put(url, json=body, headers=headers).status_code == 409
    with store.connect() as db:
        row = db.execute('SELECT data FROM project_settings_audit WHERE project_id=? ORDER BY revision DESC LIMIT 1', (p['id'],)).fetchone()
    assert '"budget_usd": null' in row[0]
    run, _ = store.create_run(p['id'], 'Monitor unpriced and expensive calls')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 10000})
    store.append(run['id'], 'usage.recorded', {'cost_usd': None, 'max_budget_usd': 10})
    budget = service._remaining_dollar_budget(run['id'], store.project(p['id']))
    assert not budget.exhausted and budget.remaining_usd is None
    assert service._usage(run['id']) == {'known_cost_usd': 10000, 'unknown_cost_calls': 1, 'calls': 2}


def test_monitoring_planner_and_worker_receive_no_ceiling(app_env, monkeypatch):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    store.update_project(p['id'], {'budget_usd': None}, p['revision'], 'owner')
    from factory.control.autonomy import DEFAULT_POLICY
    service.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'owner')
    calls = []
    original = service.runner.run
    def capture(request, emit, cancel=None):
        calls.append(request)
        assert request.max_budget_usd is None
        result = original(request, emit, cancel)
        return replace(result, cost_usd=100)
    monkeypatch.setattr(service.runner, 'run', capture)
    response = client.post('/api/v2/runs', json={'operation': 'bugfix', 'project_id': p['id'], 'request': 'Update greeting'}, headers=headers)
    assert response.status_code == 201
    rid = response.json()['id']
    run = wait_state(store, rid, {'awaiting_approval', 'needs_human'})
    assert run['status'] == 'awaiting_approval', run
    assert client.post(f'/api/v2/runs/{rid}/approve', json={'revision': run['revision']}, headers=headers).status_code == 200
    run = wait_state(store, rid, {'ready_for_review', 'needs_human'})
    assert run['status'] == 'ready_for_review', run
    assert any(call.read_only for call in calls) and any(not call.read_only for call in calls)
    assert service._usage(rid)['known_cost_usd'] >= 200


def test_monitoring_verifier_has_no_ceiling_after_large_spend(app_env, monkeypatch):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    p = store.update_project(p['id'], {'budget_usd': None}, p['revision'], 'owner')
    run, _ = store.create_run(p['id'], 'Verify')
    run['agent_snapshot'] = {'acceptance': ['Keep compatibility']}
    service.cancels[run['id']] = threading.Event()
    store.append(run['id'], 'usage.recorded', {'cost_usd': 10000})
    cfg = service.runtime_settings.get()
    cfg['agent_verification_profile'] = {'provider': 'codex', 'model': 'review'}
    calls = []
    def verify(request, emit, cancel=None):
        calls.append(request)
        return ProviderResult(passing_review(request, "observed evidence"), cost_usd=.03)
    monkeypatch.setattr(service.runner, 'run', verify)
    artifacts = {'checks': [{'name': 'unit', 'exit': 0}], 'worktree': str(repo)}
    service._independent_verify(run['id'], run, p, cfg, artifacts)
    assert calls and calls[0].max_budget_usd is None
    assert service._usage(run['id'])['known_cost_usd'] == 10000.03


def test_zip_default_monitoring_and_mode_changes_conflict_on_reused_key(app_env):
    from tests.test_project_import import archive, post
    client, store, service, repo = app_env
    headers = login(client)
    payload = archive([('README.md', 'Example')])
    result = post(client, headers, payload)
    assert result.status_code == 201, result.text
    assert result.json()['project']['budget_usd'] is None
    assert result.json()['project']['budget_source'] == 'inherit'
    assert post(client, headers, payload).json()['project']['id'] == result.json()['project']['id']
    # The zip form has no budget field; an unknown form field is rejected outright.
    assert post(client, headers, payload, name='别的名字').status_code == 409


def test_noop_settings_save_during_execution_keeps_revision_and_audit(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    run, _ = store.create_run(p['id'], 'Active work')
    store.update(run['id'], {'status': 'running'})
    before = store.project_audit(p['id'])
    result = client.put(f"/api/v2/projects/{p['id']}", headers=headers, json={
        **{k:p[k] for k in ('name','base_branch','checks','revision')},
        'auto_spec_confirm': False, 'requirement_analysis_budget_usd': None})
    assert result.status_code == 200, result.text
    assert result.json()['revision'] == p['revision']
    assert store.project_audit(p['id']) == before
    with pytest.raises(Conflict, match='进行中的运行'):
        store.update_project(p['id'], {'name':'Changed'}, p['revision'], 'owner')


def test_analysis_budget_can_be_relaxed_with_completed_delivery_pending(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    p = store.update_project(p['id'], {'requirement_analysis_budget_usd':5}, p['revision'], 'owner')
    run, _ = store.create_run(p['id'], 'Delivered work')
    store.update(run['id'], {'status':'ready_for_review'})
    updated = store.update_project(p['id'], {'requirement_analysis_budget_usd':None, 'budget_usd':None}, p['revision'], 'owner')
    assert updated['requirement_analysis_budget_usd'] is None
    assert updated['budget_usd'] is None
    assert updated['revision'] == p['revision'] + 1
    with pytest.raises(Conflict):
        store.update_project(p['id'], {'requirement_analysis_budget_usd':1}, updated['revision'], 'owner')
