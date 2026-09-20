"""Admin cost policy: inheritance, explicit override, and who may change money."""
import pytest

from factory.control.cost_policy import CostPolicy
from factory.control.store import Conflict
from tests.test_control_app import app_env, login, project


def _admin_policy(client, headers, amount, revision=0):
    return client.put('/api/v2/runtime/cost-policy', headers=headers,
                      json={'revision': revision, 'default_project_budget_usd': amount})


def _member(client, name='member1'):
    client.app.state.auth.create_user(name, 'member-long-password', role='member')
    response = client.post('/api/auth/login', json={'username': name, 'password': 'member-long-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def test_unconfigured_policy_is_monitoring_only(app_env):
    _, store, service, _ = app_env
    assert service.cost_policy.config() == {'revision': 0, 'default_project_budget_usd': None}
    # An unconfigured policy must not introduce a hidden hard stop.
    assert service.cost_policy.effective_budget_usd({'budget_usd': None, 'budget_source': 'inherit'}) is None


def test_default_none_records_usage_without_stopping(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = client.post('/api/v2/projects/create-workspace', headers=headers,
                    json={'name': '监测项目', 'idempotency_key': 'monitor-only-1'}).json()
    assert p['budget_source'] == 'inherit' and p['budget_usd'] is None
    run, _ = store.create_run(p['id'], '只监测不停机')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 9999})
    budget = service._remaining_dollar_budget(run['id'], service._enforced_project(p['id']))
    assert not budget.exhausted and budget.remaining_usd is None and budget.limit_usd is None
    assert service._usage(run['id'])['known_cost_usd'] == 9999
    # Monitoring must never claim exhaustion anywhere in the event log.
    assert not [e for e in store.events(run['id']) if e['type'] == 'budget.threshold_warning']


def test_finite_policy_applies_to_inheriting_projects_only(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    inheriting = client.post('/api/v2/projects/create-workspace', headers=headers,
                             json={'name': '继承项目', 'idempotency_key': 'inherit-1'}).json()
    explicit = project(client, repo, headers)  # registered with budget_usd=10
    assert explicit['budget_source'] == 'explicit' and explicit['budget_usd'] == 10
    assert _admin_policy(client, headers, 4.0).status_code == 200

    assert service._enforced_project(inheriting['id'])['budget_usd'] == 4.0
    # An explicit project keeps its own number; the policy does not rewrite it.
    assert service._enforced_project(explicit['id'])['budget_usd'] == 10
    assert store.project(inheriting['id'])['budget_usd'] is None  # stored row untouched


def test_legacy_rows_without_budget_source_stay_unlimited(app_env):
    _, store, service, repo = app_env
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        legacy = store._insert_project(db, {'name': '老项目', 'repository': 'owner/legacy',
                                            'workspace': str(repo), 'base_branch': 'main',
                                            'budget_usd': None, 'checks': {}})
    service.cost_policy.configure(0, 3.0)
    # No budget_source means explicit: null keeps meaning monitoring only.
    assert 'budget_source' not in store.project(legacy['id'])
    assert service._enforced_project(legacy['id'])['budget_usd'] is None


def test_policy_revision_is_compare_and_swap(app_env):
    client, _, service, _ = app_env
    headers = login(client)
    assert _admin_policy(client, headers, 5.0).json()['revision'] == 1
    assert _admin_policy(client, headers, 6.0, revision=0).status_code == 409
    assert service.cost_policy.default_budget_usd() == 5.0
    assert _admin_policy(client, headers, None, revision=1).json()['default_project_budget_usd'] is None
    for invalid in (0, -1, 1000001):
        assert _admin_policy(client, headers, invalid, revision=2).status_code == 422


def test_member_cannot_read_or_change_the_policy(app_env):
    client, store, service, repo = app_env
    admin = login(client)
    p = project(client, repo, admin)
    seat = _member(client)
    assert client.get('/api/v2/runtime/cost-policy', headers=seat).status_code == 403
    assert _admin_policy(client, seat, 50.0).status_code == 403
    # Members must not be able to raise a project ceiling either.
    body = {k: p[k] for k in ('name', 'base_branch', 'checks', 'revision')}
    denied = client.put(f"/api/v2/projects/{p['id']}", headers=seat, json={**body, 'budget_usd': 900})
    assert denied.status_code == 403 and '管理员' in denied.json()['detail']
    assert store.project(p['id'])['budget_usd'] == 10


def test_switching_a_project_to_inherit_clears_the_stored_amount(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    service.cost_policy.configure(0, 2.0)
    body = {k: p[k] for k in ('name', 'base_branch', 'checks', 'revision')}
    updated = client.put(f"/api/v2/projects/{p['id']}", headers=headers,
                         json={**body, 'budget_source': 'inherit'})
    assert updated.status_code == 200, updated.text
    assert updated.json()['budget_source'] == 'inherit'
    assert updated.json()['budget_usd'] is None
    assert service._enforced_project(p['id'])['budget_usd'] == 2.0
    # Naming an amount again is an explicit override, recorded as such.
    again = client.put(f"/api/v2/projects/{p['id']}", headers=headers,
                       json={**body, 'revision': updated.json()['revision'], 'budget_usd': 7.5})
    assert again.status_code == 200, again.text
    assert again.json()['budget_source'] == 'explicit' and again.json()['budget_usd'] == 7.5
    assert service._enforced_project(p['id'])['budget_usd'] == 7.5


def test_store_rejects_an_unknown_budget_source(app_env):
    _, store, _, repo = app_env
    p = store.add_project({'name': 'X', 'repository': 'owner/x', 'workspace': str(repo),
                           'base_branch': 'main', 'budget_usd': None, 'checks': {}})
    with pytest.raises(ValueError, match='预算来源'):
        store.update_project(p['id'], {'budget_source': 'whatever'}, p['revision'], 'owner')
