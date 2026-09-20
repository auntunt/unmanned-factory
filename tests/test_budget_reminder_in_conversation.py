"""The 80% reminder must arrive on the real task surface, for both roles."""
from tests.test_budget_reminder_and_cap import _member, _warnings
from tests.test_control_app import app_env, login, project


def _messages(client, rid):
    response = client.get(f'/api/v2/runs/{rid}/conversation')
    assert response.status_code == 200, response.text
    return response.json()['messages']


def _reminders(client, rid, text):
    return [m for m in _messages(client, rid) if text in m['content']]


def test_the_reminder_arrives_through_the_conversation_api(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))  # budget_usd == 10
    run, _ = store.create_run(p['id'], '提醒要出现在任务对话里')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    service._remaining_dollar_budget(run['id'], store.project(p['id']))
    warning = _warnings(store, run['id'])[0]
    shown = _reminders(client, run['id'], warning['payload']['message'])
    assert len(shown) == 1
    message = shown[0]
    # The message keeps its event reference, so the UI can trace it like any other.
    assert message['event_ids'] == [warning['id']]
    assert message['role'] == 'assistant' and message['at'] == warning['at']


def test_reloading_the_page_does_not_duplicate_the_reminder(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '刷新不该出现两条提醒')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    for _ in range(3):  # Each page read re-reads the budget and the conversation.
        service._remaining_dollar_budget(run['id'], store.project(p['id']))
        assert len(_reminders(client, run['id'], '本次任务已占用预算')) == 1


def test_a_member_who_owns_the_run_sees_the_same_reminder(app_env):
    client, store, service, repo = app_env
    p = project(client, repo, login(client))
    run, _ = store.create_run(p['id'], '成员也要看到提醒')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 8.5})
    service._remaining_dollar_budget(run['id'], store.project(p['id']))
    text = _warnings(store, run['id'])[0]['payload']['message']
    assert len(_reminders(client, run['id'], text)) == 1  # admin
    _member(client, service, run)  # switches the session to the member
    assert len(_reminders(client, run['id'], text)) == 1  # member


def test_a_monitoring_only_run_shows_no_budget_reminder(app_env):
    client, store, service, repo = app_env
    headers = login(client)
    p = client.post('/api/v2/projects/create-workspace', headers=headers,
                    json={'name': '仅监测对话', 'idempotency_key': 'monitor-convo-1'}).json()
    run, _ = store.create_run(p['id'], '只监测，不该提醒')
    store.append(run['id'], 'usage.recorded', {'cost_usd': 5000})
    service._remaining_dollar_budget(run['id'], service._enforced_project(p['id']))
    assert _reminders(client, run['id'], '本次任务已占用预算') == []
