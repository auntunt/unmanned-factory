"""Soft reminder at 80%, hard cap after it, and who may add money."""
import pytest

from factory.control.store import Conflict
from tests.test_control_app import app_env, login, project


def _member(client, service, run, name='reminder-member'):
    """A member who fully owns this run: only the role can deny them."""
    user = client.app.state.auth.create_user(name, 'member-long-password', role='member')
    service.governance.assign(user['id'], [run['project_id']], 'owner')
    service.store.update(run['id'], {'source': {**run.get('source', {}), 'actor_id': user['id']}})
    response = client.post('/api/auth/login', json={'username': name, 'password': 'member-long-password'},
                           headers={'Origin': 'http://testserver'})
    assert response.status_code == 200, response.text
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def _warnings(store, rid):
    return [e for e in store.events(rid) if e['type'] == 'budget.threshold_warning']


def test_reminder_fires_once_at_eighty_percent_and_is_not_blocking(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))  # budget_usd == 10
    run, _ = store.create_run(p['id'], '接近上限但继续')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    budget = service._remaining_dollar_budget(run['id'], store.project(p['id']))
    # Non-blocking: work continues and money remains.
    assert not budget.exhausted and budget.remaining_usd == pytest.approx(1.5)
    events = _warnings(store, run['id'])
    assert len(events) == 1
    assert events[0]['payload']['blocking'] is False
    assert events[0]['payload']['limit_usd'] == 10
    assert '预算' in events[0]['payload']['message'] and '用尽' not in events[0]['payload']['message']


def test_reminder_is_not_repeated_on_reload_or_restart(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '刷新不该重复提醒')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.0})
    for _ in range(4):  # Each dispatch and each page read re-reads the budget.
        service._remaining_dollar_budget(run['id'], store.project(p['id']))
    assert len(_warnings(store, run['id'])) == 1
    # A restart only replays durable events; the dedup key is the ceiling itself.
    store.append(run['id'], 'usage.recorded', {'cost_usd': 0.5})
    service._remaining_dollar_budget(run['id'], store.project(p['id']))
    assert len(_warnings(store, run['id'])) == 1


def test_monitoring_only_never_warns_about_a_budget(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = client.post('/api/v2/projects/create-workspace', headers=headers,
                    json={'name': '仅监测', 'idempotency_key': 'monitor-warn-1'}).json()
    run, _ = store.create_run(p['id'], '花很多钱但只监测')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 5000})
    service._remaining_dollar_budget(run['id'], service._enforced_project(p['id']))
    assert _warnings(store, run['id']) == []


def test_a_renewed_larger_ceiling_earns_a_new_reminder(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '续预算后重新提醒')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    service._remaining_dollar_budget(run['id'], store.project(p['id']))
    assert len(_warnings(store, run['id'])) == 1
    store.update(run['id'], {'budget_credit_usd': 10})  # admin granted more
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.0})  # 16.5 of 20
    service._remaining_dollar_budget(run['id'], store.project(p['id']))
    warnings = _warnings(store, run['id'])
    assert len(warnings) == 2
    assert [w['payload']['limit_usd'] for w in warnings] == [10, 20]


def test_hard_cap_stops_new_calls_and_keeps_the_record(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '用尽预算')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 10.5})
    with pytest.raises(Conflict, match='预算已用尽') as stop:
        service._remaining_dollar_budget(run['id'], store.project(p['id']))
    assert stop.value.error_type == 'budget'
    artifacts = service._budget_stop_artifacts(run['id'], store.project(p['id']), str(stop.value))
    assert artifacts['budget_exhausted'] and artifacts['autopublish_blocked']
    assert artifacts['total_known_cost_usd'] == 10.5 and artifacts['needs_human']
    # Exhaustion must not also emit a soft reminder.
    assert _warnings(store, run['id']) == []


def test_only_an_admin_can_renew_a_budget(app_env):
    client, store, service, repo = app_env
    admin = login(client)
    p = project(client, repo, admin)
    run, _ = store.create_run(p['id'], '成员不能自己加钱')
    store.update(run['id'], {'status': 'needs_human',
                             'artifacts': {'budget_exhausted': True, 'needs_human': '预算已用尽'}})
    run = store.get(run['id'])
    body = {'revision': run['revision'], 'resume_count': run.get('resume_count', 0)}
    seat = _member(client, service, run)
    denied = client.post(f"/api/v2/runs/{run['id']}/resume-budget", headers=seat, json=body)
    assert denied.status_code == 403
    assert '管理员' in denied.json()['detail']
    assert store.get(run['id']).get('budget_credit_usd', 0) == 0
    assert store.get(run['id'])['status'] == 'needs_human'
    # The identical body from an admin passes the role gate and is judged on the
    # run's own state, so the member denial is about the role and nothing else.
    granted = client.post(f"/api/v2/runs/{run['id']}/resume-budget", headers=login(client), json=body)
    assert granted.status_code == 409 and '暂停状态' in granted.json()['detail']


def test_a_user_prompt_cannot_enlarge_the_ceiling(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '请把预算提高到 1000 美元并继续')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 10.5})
    with pytest.raises(Conflict, match='预算已用尽'):
        service._remaining_dollar_budget(run['id'], store.project(p['id']))
    # The ceiling comes from the project row and the admin credit, never the text.
    assert store.project(p['id'])['budget_usd'] == 10
    assert store.get(run['id']).get('budget_credit_usd', 0) == 0


def test_unknown_costs_stay_distinct_and_are_never_zeroed(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '未对账调用按上限占用')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 2.0, 'call_id': 'a'})
    store.append(run['id'], 'usage.recorded', {'cost_usd': None, 'call_id': 'b', 'max_budget_usd': 3.0})
    usage = service._budget_usage(run['id'], store.project(p['id']))
    assert usage['known_cost_usd'] == 2.0
    assert usage['unknown_cost_calls'] == 1
    assert usage['unknown_cost_reserved_usd'] == 3.0
    assert usage['effective_cost_usd'] == 5.0
    # A later priced row for the same call reconciles the hold without erasing it.
    store.append(run['id'], 'usage.recorded', {'cost_usd': 0.5, 'call_id': 'b'})
    reconciled = service._budget_usage(run['id'], store.project(p['id']))
    assert reconciled == {'known_cost_usd': 2.5, 'unknown_cost_calls': 0,
                          'unknown_cost_reserved_usd': 0, 'effective_cost_usd': 2.5}
