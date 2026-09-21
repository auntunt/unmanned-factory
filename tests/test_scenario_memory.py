"""Project memory shared by the three scenarios, and what reaches an executor.

The property worth protecting: an unconfirmed observation must never arrive at
the executor looking like a requirement the customer agreed to. Everything else
here is roundtrip plumbing over the knowledge store that already existed.
"""
from __future__ import annotations

import pytest

from factory.control import scenario_memory as sm
from factory.control.knowledge import KnowledgeStore
from tests.test_control_app import app_env, login, project  # noqa: F401
from tests.test_maintenance_routes import _base_sha, _valid_request  # noqa: F401


def _project(client, repo, headers):
    return project(client, repo, headers)


def test_new_entries_are_unconfirmed_until_a_human_says_otherwise(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    entry = sm.record(store, p['id'], plugin_id='issue-maintenance',
                      topic='报表金额精度', content='导出保留两位小数', actor='owner')
    assert entry['status'] == 'candidate'
    assert entry['title'] == '[运维] 报表金额精度'
    assert entry['provenance']['source'] == 'human'


def test_recall_crosses_scenarios_by_default_and_can_be_narrowed(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='报表金额精度',
              content='导出保留两位小数', actor='owner')
    sm.record(store, p['id'], plugin_id='legacy-modernization', topic='目标数据库',
              content='客户已批准迁移到达梦 8', actor='owner', status='active')
    everything = sm.recall(store, p['id'])
    assert {e['title'] for e in everything} == {'[运维] 报表金额精度', '[信创] 目标数据库'}
    only_maintenance = sm.recall(store, p['id'], plugin_id='issue-maintenance')
    assert [e['title'] for e in only_maintenance] == ['[运维] 报表金额精度']
    confirmed = sm.recall(store, p['id'], confirmed_only=True)
    assert [e['title'] for e in confirmed] == ['[信创] 目标数据库']


def test_unconfirmed_content_never_reaches_the_prompt_body(app_env):
    """A candidate's title is visible; its content is not quotable as a requirement.

    This is the whole reason the block is split. An evaluation report that says
    "建议迁移到达梦" is not the customer approving 达梦, and an executor that
    read the two the same way would act on the wrong one.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='legacy-modernization', topic='目标数据库',
              content='客户已批准迁移到达梦 8', actor='owner', status='active',
              paths=['db/schema.sql'], commit_sha='a' * 40)
    sm.record(store, p['id'], plugin_id='legacy-modernization', topic='评估报告的建议',
              content='报告建议顺手升级到 SpringBoot3', actor='owner')
    block = sm.constraints_block(sm.recall(store, p['id']))
    assert '客户已批准迁移到达梦 8' in block
    assert '适用：db/schema.sql' in block and 'aaaaaaaaaaaa' in block
    assert '[信创] 评估报告的建议' in block
    assert '报告建议顺手升级到 SpringBoot3' not in block
    assert '不作为依据' in block


def test_empty_memory_renders_nothing_rather_than_an_empty_heading(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    assert sm.constraints_block(sm.recall(store, p['id'])) == ''


def test_a_topicless_entry_is_refused(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    with pytest.raises(ValueError):
        sm.record(store, p['id'], plugin_id='issue-maintenance', topic='  ',
                  content='x', actor='owner')
    with pytest.raises(KeyError):
        sm.title_for('not-a-scenario', '话题')


def test_a_maintenance_task_carries_the_projects_confirmed_constraints(app_env):
    """Through the real create path: the run's prompt is what the executor sees."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7', actor='owner',
              status='active')
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='待确认的猜测',
              content='怀疑是连接池配置问题', actor='owner')

    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    run = store.get(created.json()['execution_id'])
    assert '所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7' in run['request']
    assert '[运维] 待确认的猜测' in run['request']
    assert '怀疑是连接池配置问题' not in run['request']


def test_the_next_task_on_the_same_project_reuses_the_same_memory(app_env):
    """The follow-up request does not re-evaluate the repository to know this."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7', actor='owner',
              status='active')
    first = client.post('/api/v2/maintenance/tasks',
                        json=_valid_request(p, _base_sha(repo), key='first-task-key'),
                        headers=headers)
    assert first.status_code == 201, first.text
    second = client.post('/api/v2/maintenance/tasks',
                         json=_valid_request(p, _base_sha(repo), key='second-task-key'),
                         headers=headers)
    assert second.status_code == 201, second.text
    for response in (first, second):
        run = store.get(response.json()['execution_id'])
        assert '所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7' in run['request']
    assert first.json()['task_id'] != second.json()['task_id']


def test_a_constraint_confirmed_after_intake_still_reaches_the_task(app_env):
    """Memory is read when the work is dispatched, not when the form was filed."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    entry = sm.record(store, p['id'], plugin_id='issue-maintenance',
                      topic='数据库兼容', content='SQL 必须兼容达梦 8', actor='owner')
    KnowledgeStore(store).put_entry(
        p['id'], {'kind': 'decision', 'status': 'active', 'title': entry['title'],
                  'content': entry['content'], 'paths': []},
        'owner', key=entry['key'], expected_revision=entry['revision'])
    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    assert 'SQL 必须兼容达梦 8' in store.get(created.json()['execution_id'])['request']
