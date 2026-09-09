from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from factory.control.auth import AuthError, AuthStore
from factory.control.governance import Governance, GovernedRunner, QuotaExceeded
from factory.control.providers import ProviderRequest, ProviderResult, ProviderError, ProviderCancelled
from factory.control.store import Store
from tests.test_control_app import app_env, login, project, wait_state


PASSWORD = 'team-member-password'


@pytest.fixture
def team(tmp_path):
    auth = AuthStore(tmp_path / 'users.db')
    owner = auth.create_user('owner', PASSWORD)
    member = auth.create_user('member', PASSWORD, role='member')
    store = Store(tmp_path / 'control.db')
    p = store.add_project({'name': 'Example', 'repository': 'owner/example'})
    gov = Governance(auth, store)
    gov.assign(member['id'], [p['id']], 'owner')
    run, _ = store.create_run(p['id'], 'A useful task', source={'actor_id': member['id']})
    return auth, store, gov, owner, member, p, run


def test_legacy_accounts_migrate_to_admin_and_sessions_survive(tmp_path):
    path = tmp_path / 'users.db'
    auth = AuthStore(path)
    auth.create_user('owner', PASSWORD)
    token, _, _ = auth.login('owner', PASSWORD)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE users DROP COLUMN role')
        db.execute('ALTER TABLE users DROP COLUMN active')
    migrated = AuthStore(path)
    assert migrated.authenticate(token)['role'] == 'admin'


def test_last_admin_guard_disabled_login_and_session_revocation(team):
    auth, _, _, owner, member, _, _ = team
    with pytest.raises(AuthError, match='至少保留'):
        auth.update_user(owner['id'], role='member', active=True)
    token, _, _ = auth.login('member', PASSWORD)
    auth.update_user(member['id'], role='member', active=False)
    assert auth.authenticate(token) is None
    with pytest.raises(AuthError, match='invalid credentials'):
        auth.login('member', PASSWORD)
    auth.update_user(member['id'], role='member', active=True, password='new-team-password')
    assert auth.login('member', 'new-team-password')[2]['active']


def test_password_change_requires_current_and_revokes_all_sessions(team):
    auth, _, _, _, member, _, _ = team
    tokens = [auth.login('member', PASSWORD)[0] for _ in range(2)]
    with pytest.raises(AuthError):
        auth.change_password(member['id'], 'incorrect', 'new-team-password')
    auth.change_password(member['id'], PASSWORD, 'new-team-password')
    assert all(auth.authenticate(token) is None for token in tokens)
    assert auth.login('member', 'new-team-password')


def test_atomic_reservation_prevents_parallel_oversubscription(team):
    _, _, gov, _, member, _, run = team
    gov.set_limit('member', member['id'], 20000, 'owner')
    gate = threading.Barrier(2)

    def reserve():
        gate.wait()
        try:
            return gov.reserve(run['id'], 'codex', 'test')
        except QuotaExceeded:
            return None

    with ThreadPoolExecutor(2) as pool:
        calls = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(call is not None for call in calls) == 1
    quota = gov.summary(member)['members'][0]['quota']
    assert quota['reserved_tokens'] == 20000 and quota['remaining_tokens'] == 0


def test_completed_usage_settles_once_and_overrun_blocks_next_dispatch(team):
    _, _, gov, _, member, _, run = team
    gov.set_limit('member', member['id'], 30000, 'owner')
    call = gov.reserve(run['id'], 'codex', 'test')
    gov.settle(call['id'], 40000)
    gov.settle(call['id'], 1)
    quota = gov.summary(member)['members'][0]['quota']
    assert quota['used_tokens'] == 40000 and quota['reserved_tokens'] == 0
    with pytest.raises(QuotaExceeded):
        gov.reserve(run['id'], 'codex', 'test')


def test_missing_usage_survives_month_boundary_and_reconciliation_is_audited(team, monkeypatch):
    _, _, gov, owner, member, _, run = team
    gov.set_limit('member', member['id'], 100000, 'owner')
    call = gov.reserve(run['id'], 'codex', 'test')
    gov.recover()
    monkeypatch.setattr('factory.control.governance.month_now', lambda: '2099-02')
    quota = gov.summary(member)['members'][0]['quota']
    assert quota['unknown_calls'] == 1 and quota['reserved_tokens'] == 20000
    with pytest.raises(QuotaExceeded, match='待核对'):
        gov.reserve(run['id'], 'codex', 'test')
    gov.reconcile(call['id'], 15000, '核对 SDK 的完整用量记录', 'owner')
    with pytest.raises(AuthError):
        gov.reconcile(call['id'], 0, '重复核对', 'owner')
    summary = gov.summary(owner)
    assert summary['audit'][0]['action'] == 'usage.reconciled'
    assert summary['members'][1]['quota']['reserved_tokens'] == 0
    assert summary['members'][1]['quota']['used_tokens'] == 0  # charged in dispatch month


def test_revocation_is_checked_at_dispatch_and_system_work_uses_project_and_workspace_quotas(team):
    _, store, gov, _, member, p, run = team
    gov.assign(member['id'], [], 'owner')
    with pytest.raises(QuotaExceeded, match='权限'):
        gov.reserve(run['id'], 'codex', 'test')
    system, _ = store.create_run(p['id'], 'A webhook job', source={'type': 'github'})
    gov.set_limit('project', p['id'], 0, 'owner')
    with pytest.raises(QuotaExceeded, match='项目额度'):
        gov.reserve(system['id'], 'codex', 'test')
    gov.set_limit('project', p['id'], None, 'owner')
    gov.set_limit('workspace', 'all', 0, 'owner')
    with pytest.raises(QuotaExceeded, match='工作区额度'):
        gov.reserve(system['id'], 'codex', 'test')


@pytest.mark.parametrize('incoming,outgoing', [(100, 20), (None, 20), (-1, 20), (True, 20)])
def test_wrapper_preserves_provider_usage_without_local_accounting(team, incoming, outgoing):
    _, _, gov, _, member, _, run = team

    class Runner:
        def run(self, request, emit, cancel=None):
            return ProviderResult('done', tokens_in=incoming, tokens_out=outgoing, cached_input_tokens=80)

    result = GovernedRunner(Runner(), gov, run['id']).run(ProviderRequest('codex', 'test', 'do work', '.'), lambda *_: None)
    assert result.text == 'done'
    assert result.tokens_in == incoming and result.tokens_out == outgoing
    assert result.cached_input_tokens == 80
    assert gov.summary(member)['calls'] == []  # gateway owns token settlement


def test_wrapper_preserves_cancellation_and_provider_errors_without_quota_block(team):
    _, _, gov, _, member, _, run = team
    entered, events = [], []

    class Runner:
        def run(self, request, emit, cancel=None):
            entered.append(True)
            emit('provider.usage', {'input_tokens': 5, 'output_tokens': 1})
            raise ProviderError('interrupted after one turn')

    wrapper = GovernedRunner(Runner(), gov, run['id'])
    req = ProviderRequest('codex', 'test', 'do work', '.')
    gov.set_limit('member', member['id'], 0, 'owner')
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProviderCancelled):
        wrapper.run(req, lambda *_: None, cancel=cancelled)
    assert not entered and not gov.summary(member)['calls']
    with pytest.raises(ProviderError, match='interrupted after one turn'):
        wrapper.run(req, lambda kind, payload: events.append((kind, payload)))
    assert entered == [True]
    assert events == [('provider.usage', {'input_tokens': 5, 'output_tokens': 1})]
    assert not gov.summary(member)['calls']


def test_team_api_role_assignment_ownership_and_audit(app_env):
    client, store, svc, repo = app_env
    admin_headers = login(client)
    p = project(client, repo, admin_headers)
    from factory.control.autonomy import DEFAULT_POLICY
    svc.policies.update(p['id'], {**DEFAULT_POLICY, 'mode': 'supervised'}, 0, 'owner')
    member = client.post('/api/v3/team/members', json={'username': 'member', 'password': PASSWORD}, headers=admin_headers).json()
    other = client.post('/api/v3/team/members', json={'username': 'another', 'password': PASSWORD}, headers=admin_headers).json()
    assert member['role'] == 'member'
    assert 'password' not in client.get('/api/v3/team').text
    response = client.post('/api/auth/login', json={'username': 'member', 'password': PASSWORD}, headers={'Origin': 'http://testserver'})
    headers = {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}
    for method, path, body in [
        ('post', '/api/v2/projects', {}), ('put', '/api/v2/runtime/profiles', {}),
        ('post', '/api/v3/team/members', {'username': 'hacker', 'password': PASSWORD, 'role': 'admin'}),
        ('put', '/api/v3/team/quotas', {'scope': 'member', 'scope_id': str(member['id']), 'limit_tokens': None}),
        ('post', '/api/v2/runtime/probe', {}), ('post', '/api/future/new-write', {}),
    ]:
        assert getattr(client, method)(path, json=body, headers=headers).status_code == 403
    body = {'project_id': p['id'], 'request': 'Update greeting'}
    assert client.post('/api/v2/runs', json=body, headers=headers).status_code == 403
    assert client.get('/api/v2/projects').status_code == 200
    assert [m['id'] for m in client.get('/api/v3/team').json()['members']] == [member['id']]
    svc.governance.assign(member['id'], [p['id']], 'owner')
    svc.governance.set_limit('member', member['id'], 0, 'owner')
    response = client.post('/api/v2/runs', json={**body, 'actor_id': other['id']}, headers=headers)
    assert response.status_code == 422
    response = client.post('/api/v2/runs', json=body, headers=headers)
    assert response.status_code == 201, response.text
    run = wait_state(store, response.json()['id'], {'awaiting_approval', 'needs_human', 'failed'})
    assert run['status'] == 'awaiting_approval'
    assert run['source']['actor_id'] == member['id']
    # Local quota fields are legacy audit configuration, not dispatch gates.
    assert not client.get('/api/v3/team').json()['calls']
    event_types = [event['type'] for event in store.events(run['id'])]
    assert 'quota.reserved' not in event_types
    assert 'provider.started' in event_types
    assert 'usage.recorded' in event_types
    other_run, _ = store.create_run(p['id'], 'Someone else task', source={'actor_id': other['id']})
    assert client.post(f'/api/v2/runs/{other_run["id"]}/cancel', headers=headers).status_code == 403
    assert client.post(f'/api/v2/runs/{run["id"]}/publish', headers=headers).status_code == 403


def test_http_password_reset_disables_existing_session(app_env):
    client, _, _, _ = app_env
    headers = login(client)
    response = client.post('/api/auth/password', json={'current_password': 'a-long-test-password', 'password': PASSWORD}, headers=headers)
    assert response.status_code == 200
    assert client.get('/api/v3/team').status_code == 401
