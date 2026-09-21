"""The maintenance-loop closure SHARED.md and 02-MAINTENANCE.md ask for:

"更新受影响项目记忆；下一个会话/任务能够接着工作，不全仓重新评估。" This is the write
side of that sentence -- ``issue_maintenance_webuddy.submit`` already reads project
memory into the executor prompt; this file is where a completed delivery writes a
fact back, and where the next task (or a reopened process) reads it again.

The completion example in 02-MAINTENANCE.md is the shape pinned here: two related
tasks against one project (a fix, then a small follow-on need) each produce a real
memory fact; the second task can already see the first's; a confirmed project
constraint is visible to both without being re-derived; a new delivery's commit
shows up as an update to what the project remembers; and a reopened process (a new
``MaintenanceTasks`` over the same database, standing in for a new session) still
reads everything.

The fakes are the same ones ``test_issue_maintenance_core`` already defines --
importing them, not redefining a second set, is what keeps this test honest about
which execution/repository/identity contract it is exercising.
"""
import pytest

from factory.control import scenario_memory
from factory.control.issue_maintenance import (
    MaintenanceTasks, paths_from_patch, _looks_like_a_safe_relpath)
from factory.control.store import Store
from tests.test_issue_maintenance_core import (
    ACTOR, BASE, _Execution, _Identity, _Repository, _request, _seed_project)


class _PatchedExecution(_Execution):
    """The same counting fake, but a test can choose what a delivery's patch says.

    ``export_artifacts`` in the base fake always returns the same placeholder
    bytes, which have no ``diff --git`` header for ``paths_from_patch`` to read.
    A real delivery's patch does, so this is what stands in for it here.
    """

    def __init__(self):
        super().__init__()
        self.patches = {}

    def export_artifacts(self, eid):
        return {'diff_hash': 'x' * 64,
                'artifacts': [{'name': f'{eid}.patch',
                               'bytes': self.patches.get(eid, b'--- patch ---\n')}]}


def _patch_bytes(*paths):
    """Minimal bytes shaped like a real ``git format-patch`` for the given files."""
    lines = []
    for path in paths:
        lines += [f'diff --git a/{path} b/{path}', f'--- a/{path}', f'+++ b/{path}',
                  '@@ -1 +1 @@', '-old', '+new']
    return ('\n'.join(lines) + '\n').encode()


@pytest.fixture
def env(tmp_path):
    db = tmp_path / 'control.db'
    store = Store(db)
    _seed_project(store)
    execution = _PatchedExecution()
    port = MaintenanceTasks(store, execution=execution,
                            repository=_Repository(), identity=_Identity())
    port.fake = execution
    return store, db, port


def _entry_titles(refs):
    return [ref['title'] for ref in refs]


def test_two_related_tasks_share_confirmed_constraints_and_each_add_a_fact(env):
    store, db, port = env

    # A project constraint a human already confirmed -- what ``constraints_block``
    # would carry into an executor prompt as a requirement, not a suggestion.
    scenario_memory.record(
        store, 'p1', plugin_id='issue-maintenance', topic='数据库兼容',
        content='报表模块必须兼容达梦数据库的日期函数', actor='reviewer',
        status=scenario_memory.CONFIRMED_STATUS, paths=['db/schema.sql'])

    # Task 1: a defect fix.
    fix = port.create(_request(idempotency_key='fix-issue-01', issue={
        'external_id': '2001', 'title': '报表金额显示修复',
        'body': '汇总行金额显示为空。'}), actor=ACTOR)
    port.fake.patches[fix['execution_id']] = _patch_bytes('report.py')
    port.fake.deliver(fix['execution_id'], commit='b' * 40)
    port.export(fix['task_id'], actor=ACTOR)

    fix_view = port.get(fix['task_id'], actor=ACTOR)
    refs = fix_view['project_memory']
    assert '[运维] 数据库兼容' in _entry_titles(refs), '已确认的项目约束必须能被复用'
    confirmed = next(r for r in refs if r['title'] == '[运维] 数据库兼容')
    assert confirmed['status'] == scenario_memory.CONFIRMED_STATUS
    fix_fact = next(r for r in refs if r['title'] == '[运维] 报表金额显示修复')
    assert fix_fact['status'] == scenario_memory.UNCONFIRMED_STATUS, \
        '系统观察到的交付事实默认未确认'
    assert fix_fact['paths'] == ['report.py']
    assert fix_fact['commit_sha'] == 'b' * 40

    # Task 2: a related small ask, filed after the fix. It must see the fix's
    # own fact and the confirmed constraint without anything having been
    # re-evaluated for it -- this is "下一个任务能接着工作".
    feature = port.create(_request(idempotency_key='feature-issue-01', issue={
        'external_id': '2002', 'title': '报表增加区间筛选',
        'body': '希望能按日期区间筛选导出结果。'}), actor=ACTOR)
    before = _entry_titles(port.get(feature['task_id'], actor=ACTOR)['project_memory'])
    assert '[运维] 数据库兼容' in before
    assert '[运维] 报表金额显示修复' in before, '相关任务应能读到前一个任务留下的事实'

    port.fake.patches[feature['execution_id']] = _patch_bytes('report.py', 'filters.py')
    port.fake.deliver(feature['execution_id'], commit='c' * 40)
    port.export(feature['task_id'], actor=ACTOR)

    after = port.get(feature['task_id'], actor=ACTOR)['project_memory']
    titles = _entry_titles(after)
    # A new code version is now part of what the project remembers.
    assert titles.count('[运维] 数据库兼容') == 1
    assert titles.count('[运维] 报表金额显示修复') == 1
    assert titles.count('[运维] 报表增加区间筛选') == 1
    feature_fact = next(r for r in after if r['title'] == '[运维] 报表增加区间筛选')
    assert feature_fact['commit_sha'] == 'c' * 40
    assert feature_fact['paths'] == ['report.py', 'filters.py']

    # Re-exporting the fix (a stale retry, a reopened UI tab) must not double
    # the fact it already wrote -- "同一个 (task_id, revision) 重复导出不能写出
    # 第二条记忆条目".
    port.fake.deliver(fix['execution_id'], commit='d' * 40)
    port.export(fix['task_id'], actor=ACTOR)
    recorded = scenario_memory.recall(store, 'p1', plugin_id='issue-maintenance')
    assert [e['title'] for e in recorded].count('[运维] 报表金额显示修复') == 1
    # And the receipt is unaffected: the original commit, not the retried one.
    still_first_commit = next(
        e for e in recorded if e['title'] == '[运维] 报表金额显示修复')['commit_sha']
    assert still_first_commit == 'b' * 40

    # A reopened process -- a new ``MaintenanceTasks`` over the same database,
    # standing in for a new session -- still reads the confirmed requirement
    # and both delivered facts, without re-deriving any of it.
    reopened = MaintenanceTasks(Store(db), execution=_PatchedExecution(),
                                repository=_Repository(), identity=_Identity())
    followup = reopened.create(_request(idempotency_key='followup-issue-01', issue={
        'external_id': '2003', 'title': '补充需求：导出格式对齐财务口径',
        'body': '延续前两次改动，财务希望导出格式统一。'}), actor=ACTOR)
    seen = _entry_titles(reopened.get(followup['task_id'], actor=ACTOR)['project_memory'])
    assert '[运维] 数据库兼容' in seen, '新会话仍要读到已确认要求'
    assert '[运维] 报表金额显示修复' in seen
    assert '[运维] 报表增加区间筛选' in seen


def test_a_project_with_no_memory_yet_is_an_empty_list_not_a_broken_view(env):
    """A brand-new project has nothing to recall; the view must still render."""
    _store, _db, port = env
    view = port.create(_request(idempotency_key='no-memory-yet'), actor=ACTOR)
    assert view['project_memory'] == []


def test_an_unresolvable_project_does_not_break_reading_the_task(tmp_path):
    """``_project_memory`` must degrade to empty, not raise, on a project KeyError.

    Deliberately does *not* seed the project this time: reading a task must not
    depend on the knowledge store's own project bookkeeping being present.
    """
    store = Store(tmp_path / 'control.db')
    execution = _PatchedExecution()
    port = MaintenanceTasks(store, execution=execution,
                            repository=_Repository(), identity=_Identity())
    view = port.create(_request(idempotency_key='ghost-project'), actor=ACTOR)
    assert view['project_memory'] == []


def test_paths_from_patch_reads_the_new_side_of_each_diff_header():
    patch = _patch_bytes('a/b.py', 'c.py') + b'\n' + _patch_bytes('c.py')
    assert paths_from_patch(
        [{'name': 'x.patch', 'bytes': patch}]) == ['a/b.py', 'c.py']


def test_paths_from_patch_ignores_non_bytes_and_empty_artifacts():
    assert paths_from_patch(None) == []
    assert paths_from_patch([]) == []
    assert paths_from_patch([{'name': 'x', 'bytes': 'not bytes'}]) == []


@pytest.mark.parametrize('path,safe', [
    ('report.py', True), ('a/b/c.py', True),
    ('/etc/passwd', False), ('../secret', False), ('a/../b', False),
    ('a\\b', False), ('', False),
])
def test_looks_like_a_safe_relpath(path, safe):
    assert _looks_like_a_safe_relpath(path) is safe


def test_the_view_field_is_present_even_before_any_delivery(env):
    """M4 needs one stable typed shape; ``project_memory`` is part of it from turn one."""
    _store, _db, port = env
    view = port.create(_request(idempotency_key='shape-check'), actor=ACTOR)
    assert 'project_memory' in view
    assert isinstance(view['project_memory'], list)


def test_an_unusually_long_issue_title_does_not_block_the_export(env):
    """The knowledge store caps a title at 200 chars; nothing upstream of it does.

    Before ``_remember_delivery`` truncated its inputs, a legitimate delivery
    for a verbosely-titled issue would raise out of ``scenario_memory.record``
    -- inside ``export``, after the receipt had already been computed -- and
    the caller would see a delivered task it could never get a receipt for.
    """
    _store, _db, port = env
    long_title = '报表导出在跨月区间下偶发丢行' * 20  # well past 200 chars
    view = port.create(_request(idempotency_key='long-title-01', issue={
        'external_id': '3001', 'title': long_title,
        'body': '标题异常长，用来钉住截断而不是拒绝。'}), actor=ACTOR)
    port.fake.deliver(view['execution_id'])
    exported = port.export(view['task_id'], actor=ACTOR)
    assert exported['receipt']['delivery']['commit'] == 'c' * 40
    refs = port.get(view['task_id'], actor=ACTOR)['project_memory']
    assert any(ref['title'].startswith('[运维] 报表导出') for ref in refs)
