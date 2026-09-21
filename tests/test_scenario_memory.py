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


def _binding_section(block: str) -> str:
    """Only the text under 「本次必须遵守」, which is the one binding heading."""
    if '本次必须遵守' not in block:
        return ''
    after = block.split('本次必须遵守', 1)[1]
    # The next role heading ends the binding section.
    for heading in ('（供参考', '（只说明存在这个问题'):
        if heading in after:
            after = after.split(heading, 1)[0]
    return after


def test_unconfirmed_material_is_readable_but_never_binding(app_env):
    """An unconfirmed entry must be *readable* and must not be *an order*.

    The first version solved this by showing only the title, which made an open
    question permanently unreadable -- the reviewer's point. Now the content
    travels, but under a heading that says it is not a basis for a decision, and
    the assertion is about which section it lands in rather than about whether
    the words appear at all. "建议迁移到达梦" is not the customer approving 达梦.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='legacy-modernization', topic='目标数据库',
              content='客户已批准迁移到达梦 8', actor='owner', kind='decision',
              status='active', paths=['db/schema.sql'], commit_sha='a' * 40)
    sm.record(store, p['id'], plugin_id='legacy-modernization', topic='评估报告的建议',
              content='报告建议顺手升级到 SpringBoot3', actor='owner')
    block = sm.constraints_block(sm.recall(store, p['id']))
    binding = _binding_section(block)
    # The confirmed decision is binding, with its scope and code version.
    assert '客户已批准迁移到达梦 8' in binding
    assert '适用：db/schema.sql' in block and 'aaaaaaaaaaaa' in block
    # The unconfirmed suggestion is present and readable...
    assert '报告建议顺手升级到 SpringBoot3' in block
    # ...but not under the heading that says "must obey".
    assert '报告建议顺手升级到 SpringBoot3' not in binding
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
    # Binding: a confirmed decision. ``status='active'`` alone is not enough --
    # a confirmed *fact* is background, and folding the two together is what
    # turned every confirmed row into an order in the first version.
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7', actor='owner',
              kind='decision', status='active')
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='待确认的猜测',
              content='怀疑是连接池配置问题', actor='owner')

    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    run = store.get(created.json()['execution_id'])
    binding = _binding_section(run['request'])
    assert '所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7' in binding
    # The unconfirmed guess travels as readable context, never as a requirement.
    assert '怀疑是连接池配置问题' in run['request']
    assert '怀疑是连接池配置问题' not in binding
    # And the dispatch froze exactly what it loaded.
    refs = run['source']['memory_refs']
    assert {r['role'] for r in refs} == {'constraint', 'observation'}


def test_the_next_task_on_the_same_project_reuses_the_same_memory(app_env):
    """The follow-up request does not re-evaluate the repository to know this."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='所有 SQL 必须同时兼容达梦 8 与 MySQL 5.7', actor='owner',
              kind='decision', status='active')
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


def test_what_a_running_task_was_dispatched_against_does_not_move(app_env):
    """A confirmation made after dispatch must not rewrite the running task's basis.

    New requirements are supposed to arrive through the supplement/revision
    flow, where a human sees them land. Silently changing what an in-flight run
    is being judged against is the opposite: the run keeps going, and the
    record of why it did what it did becomes untrue.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='SQL 必须兼容达梦 8', actor='owner', kind='decision',
              status='active')

    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    task_id = created.json()['task_id']

    frozen = client.get(f'/api/v2/maintenance/tasks/{task_id}',
                        headers=headers).json()['project_memory']
    assert frozen['frozen'] is True, frozen
    loaded_keys = {e['key'] for e in frozen['entries']}
    assert len(loaded_keys) == 1

    # A brand-new requirement is confirmed while the task is already running.
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='新要求',
              content='导出必须保留两位小数', actor='owner', kind='decision',
              status='active')

    after = client.get(f'/api/v2/maintenance/tasks/{task_id}',
                       headers=headers).json()['project_memory']
    assert after['frozen'] is True
    assert {e['key'] for e in after['entries']} == loaded_keys, \
        '已经派发的任务不能因为项目后来确认了新要求就改变依据'
    # The project does know the new requirement -- it is simply not this run's basis.
    assert len(sm.recall(store, p['id'], confirmed_only=True)) == 2


def test_an_out_of_scope_requirement_is_not_loaded_but_is_counted(app_env):
    """Confirming something does not make it every task's problem.

    A requirement scoped to ``db/`` is not a requirement about a change in
    ``web/``. It is left out of the prompt and reported as left out, so the
    reader can tell "not relevant here" from "the project never said this".
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='数据库兼容',
              content='SQL 必须兼容达梦 8', actor='owner', kind='decision',
              status='active', paths=['db/schema.sql'])
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='全局要求',
              content='所有改动都要带回归测试', actor='owner', kind='decision',
              status='active')

    loaded = sm.load_for_task(store, p['id'], scope_paths=('web/report.py',))
    titles = {e['title'] for e in loaded['entries']}
    assert '[运维] 全局要求' in titles, '没有声明范围的要求对每次改动都适用'
    assert '[运维] 数据库兼容' not in titles
    assert any(d['reason'] == 'out_of_scope' for d in loaded['dropped'])
    assert '适用范围与本次改动无关' in loaded['block']


def test_memory_past_the_budget_is_dropped_loudly_and_requirements_go_first(app_env):
    """A large project must not push the task out of the window silently."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='关键要求',
              content='必须保留两位小数', actor='owner', kind='decision',
              status='active')
    for i in range(12):
        sm.record(store, p['id'], plugin_id='issue-maintenance',
                  topic=f'历史观察{i}', content='x' * 400, actor='owner')

    loaded = sm.load_for_task(store, p['id'], budget_chars=1200)
    titles = [e['title'] for e in loaded['entries']]
    # The binding requirement survives the budget; observations are what go.
    assert '[运维] 关键要求' in titles
    assert len(titles) < 13
    assert any(d['reason'] == 'budget' for d in loaded['dropped'])
    assert '因长度上限未加载' in loaded['block']
    assert '不要假设它们不存在' in loaded['block']


# --- the memory defects the cb74bab review reproduced ----------------------
def test_one_oversized_requirement_does_not_sail_past_the_budget(app_env):
    """The review's probe: a 7000-char active decision with a 6000-char budget
    loaded whole (7069 chars actually carried) and then pushed the *next*
    binding requirement out. Both halves of that were wrong."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='巨大要求',
              content='要' * 7000, actor='owner', kind='decision', status='active')
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='第二条要求',
              content='导出保留两位小数', actor='owner', kind='decision',
              status='active')

    loaded = sm.load_for_task(store, p['id'], budget_chars=6000)
    # Not "carried anyway": the task is blocked, with the offending entries named.
    assert loaded['blocked'], loaded
    assert loaded['blocked']['kind'] == 'memory.constraints_exceed_budget'
    assert loaded['entries'] == [] and loaded['refs'] == []
    titles = {e['title'] for e in loaded['blocked']['entries']}
    assert titles == {'[运维] 巨大要求', '[运维] 第二条要求'}
    assert '把任务切小' in loaded['blocked']['message']


def test_a_binding_requirement_is_never_dropped_to_make_room(app_env):
    """Context gives way to the budget; requirements do not."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    for i in range(6):
        sm.record(store, p['id'], plugin_id='issue-maintenance', topic=f'要求{i}',
                  content='必须' + 'x' * 200, actor='owner', kind='decision',
                  status='active')
    for i in range(6):
        sm.record(store, p['id'], plugin_id='issue-maintenance', topic=f'背景{i}',
                  content='y' * 400, actor='owner')

    loaded = sm.load_for_task(store, p['id'], budget_chars=1800)
    assert loaded['blocked'] is None, loaded
    roles = [r['role'] for r in loaded['refs']]
    assert roles.count('constraint') == 6, '六条必遵要求一条都不能少'
    assert all(d['role'] != 'constraint' for d in loaded['dropped'])
    assert any(d['reason'] == 'budget' for d in loaded['dropped'])
    # And the budget really binds the rest.
    assert loaded['chars'] <= 1800 + sum(
        len(e['content']) for e in loaded['entries'] if sm.role_of(e) == 'constraint')


def test_a_requirement_is_never_shortened_into_a_summary(app_env):
    """A truncated must-obey line is a requirement nobody can actually follow."""
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='范围内要求',
              content='必须' + 'x' * 600, actor='owner', kind='decision',
              status='active', paths=['db/schema.sql'])
    for i in range(20):
        sm.record(store, p['id'], plugin_id='issue-maintenance', topic=f'背景{i}',
                  content='y' * 500, actor='owner', paths=[f'web/m{i}.py'])

    loaded = sm.load_for_task(store, p['id'], scope_paths=None, budget_chars=1500)
    constraint = next(e for e in loaded['entries'] if sm.role_of(e) == 'constraint')
    assert constraint.get('summary_only') is not True
    assert constraint['content'].endswith('x' * 10)
    assert any(r.get('summary_only') for r in loaded['refs']) or loaded['dropped']
    assert '必须遵守的要求都是完整的' in loaded['block']


def test_maintenance_says_its_scope_is_unknown_and_narrows_it_after_the_plan(app_env):
    """An issue declares no files, so no relevance filtering happened at intake.

    That is recorded rather than implied, and ``refine_memory_for_plan`` is what
    narrows memory once an approved plan names the files.
    """
    client, store, svc, repo = app_env
    headers = login(client)
    p = _project(client, repo, headers)
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='报表相关',
              content='导出保留两位小数', actor='owner', kind='decision',
              status='active', paths=['report.py'])
    sm.record(store, p['id'], plugin_id='issue-maintenance', topic='登录相关',
              content='登录页不要改', actor='owner', kind='decision',
              status='active', paths=['login.py'])

    created = client.post('/api/v2/maintenance/tasks',
                          json=_valid_request(p, _base_sha(repo)), headers=headers)
    assert created.status_code == 201, created.text
    run_id = created.json()['execution_id']
    source = store.get(run_id)['source']
    assert source['memory_scope_known'] is False
    assert len(source['memory_refs']) == 2, '范围未知时不猜，两条都带上'

    # A plan arrives naming one file; memory narrows to it.
    store.update(run_id, {'status': 'awaiting_approval',
                          'plan': {'title': '修导出', 'tasks': [
                              {'id': 't1', 'title': '改导出', 'paths': ['report.py']}]}})
    from factory.control.issue_maintenance_webuddy import tasks_for
    port = tasks_for(svc)
    refined = port.port.execution.refine_memory_for_plan(run_id)
    assert refined['refined'] is True, refined
    assert refined['paths'] == ['report.py']
    kept = {r['title'] for r in refined['refs']}
    assert '[运维] 报表相关' in kept
    assert '[运维] 登录相关' not in kept
    # Both bases are kept: what the executor was given, and what a reviewer
    # should read the result against.
    after = store.get(run_id)['source']
    assert len(after['memory_refs']) == 2
    assert len(after['memory_refs_refined']) == 1
