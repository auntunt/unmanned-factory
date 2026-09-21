"""A maintenance task survives a restart, and the restart is the real one.

The fake execution port in ``test_issue_maintenance_core`` can prove that the
module reads its status from the execution rather than from a column, but it
cannot prove that the thing being read survives the process that wrote it.  So
this closes the first coordinator, builds a second ``Store``/``Service`` over the
same ``control.db``, runs the existing ``recover()`` -- M2's path, not a second
recovery engine -- and only then asks the maintenance surface what happened.

What is checked, in the failure direction each time:

* The interrupted run is mapped to ``waiting`` by the existing vocabulary, not by
  a status this module stored before the crash.
* The blocking reason cites the recovery event that actually exists in the log,
  so a reader can go look at it.
* Cost, interventions and the receipt are still there after reopening, and no
  second execution is dispatched when the same key is imported again.

No paid call happens: nothing here dispatches a provider at all.
"""
from __future__ import annotations

import pytest

from factory.control.issue_maintenance import MaintenanceTasks
from factory.control.issue_maintenance_webuddy import (
    WebuddyExecution, WebuddyIdentity, WebuddyRepository)
from factory.control.service import Service
from factory.control.store import Store

ACTOR = {'id': 5, 'username': 'operator'}


def _repo(tmp_path):
    """A real git repository, because the baseline is resolved against git."""
    import subprocess
    root = tmp_path / 'repo'
    root.mkdir()
    for args in (['init', '-b', 'main'], ['config', 'user.email', 'f@l'],
                 ['config', 'user.name', 'F']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'README.md').write_text('legacy\n')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'base'], cwd=root, check=True,
                   capture_output=True)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    return root, base


def _tasks(store, *, dispatch):
    """The same wiring ``tasks_for`` builds, with dispatch under test control."""
    return MaintenanceTasks(
        store,
        execution=WebuddyExecution(store, dispatch=dispatch,
                                   cost=lambda eid: 0.0),
        repository=WebuddyRepository(store),
        identity=WebuddyIdentity())


def _request(base, project_id, **over):
    return {'issue': {'source': 'manual-synthetic', 'external_id': '7',
                      'version': '1', 'title': '重启后要找回同一个任务',
                      'body': '合成用例：导入后服务重启。'},
            'project_id': project_id, 'repository': 'synthetic/recovery',
            'base_sha': base, 'base_branch_label': 'main',
            'expected_behaviour': '重启后同一 task 仍可查', 'delivery_goal': '补丁与回执',
            'agreement': {'revision': 'v4', 'skill_version': 'issue-maintenance@2'},
            'idempotency_key': 'restart-case-1', 'synthetic': True, **over}


class _NeverRuns:
    """A runner whose being called at all is the failure."""

    def run(self, *a, **kw):  # pragma: no cover - reaching this is the bug
        raise AssertionError('恢复用例不应触发任何编码调用')


def _service(tmp_path, monkeypatch):
    """A coordinator over the durable file; a restart builds a second one."""
    store = Store(tmp_path / 'control.db')
    svc = Service(store, runner=_NeverRuns(),
                  profiles={role: {'provider': 'codex', 'model': 'test'}
                            for role in ('planner', 'cheap', 'standard', 'strong')})
    monkeypatch.setattr(svc, '_ensure_scheduler', lambda: None)
    monkeypatch.setattr(svc, '_submit', lambda *a, **kw: None)
    return store, svc


@pytest.fixture
def imported(tmp_path, monkeypatch):
    """One imported maintenance task whose execution was interrupted mid-run."""
    root, base = _repo(tmp_path)
    store, svc = _service(tmp_path, monkeypatch)
    project = store.add_project({
        'name': '合成恢复仓库', 'repository': 'synthetic/recovery',
        'workspace': str(root), 'base_branch': 'main', 'budget_usd': 5.0,
        'checks': {}, 'max_tasks': 1})
    dispatched = []
    tasks = _tasks(store, dispatch=dispatched.append)
    view = tasks.create(_request(base, project['id']), actor=ACTOR)
    # Interrupted, exactly as a crash leaves it: a durable ACTIVE row and no
    # terminal event. Nothing here pretends to know the task is finished.
    store.update(view['execution_id'], {'status': 'running'}, expected=('received',))
    svc.close()
    return {'tmp_path': tmp_path, 'task_id': view['task_id'],
            'execution_id': view['execution_id'], 'project_id': project['id'],
            'base': base, 'dispatched': dispatched}


def test_a_restart_finds_the_same_task_and_says_why_it_is_stopped(imported, monkeypatch):
    """Reopened database, existing recover(), and only then the maintenance view.

    Asking the service that wrote the row cannot distinguish "durably recorded"
    from "still in this process's memory", so the first coordinator is closed and
    a second one is built over the same file.
    """
    store, svc = _service(imported['tmp_path'], monkeypatch)
    svc.recover()  # M2's path, reused as-is
    tasks = _tasks(store, dispatch=lambda rid: pytest.fail('恢复不应派发新执行'))

    view = tasks.get(imported['task_id'], actor=ACTOR)
    assert view['execution_id'] == imported['execution_id'], '重启后必须是同一次执行'
    assert view['status'] == 'waiting', f"实际 {view['status']}"

    # The reason is the event recovery actually wrote, referenced so it can be read.
    reason = view['blocking_reason']
    assert reason['kind'] == 'run.recovered', reason
    assert reason['message'], '阻塞原因不能是空字符串'
    assert reason['event']['execution_id'] == imported['execution_id']
    cited = [event for event in tasks.events(imported['task_id'], actor=ACTOR)
             if event['sequence'] == reason['event']['sequence']]
    assert [event['kind'] for event in cited] == ['run.recovered'], \
        '引用的事件序号必须真的指向那条事件'


def test_reimporting_the_same_key_after_a_restart_dispatches_nothing_new(imported,
                                                                        monkeypatch):
    """The idempotency record is in the database, so it outlives the process.

    ``dispatch`` here fails the test if it is called at all: the point is not that
    the second import returns the same id, it is that nothing was started for it.
    """
    store, svc = _service(imported['tmp_path'], monkeypatch)
    svc.recover()
    tasks = _tasks(store, dispatch=lambda rid: pytest.fail('重启后重复导入不应再派发'))
    again = tasks.create(_request(imported['base'], imported['project_id']), actor=ACTOR)
    assert again['task_id'] == imported['task_id']
    assert again['execution_id'] == imported['execution_id']


def test_cost_and_supplements_recorded_before_the_restart_are_still_there(imported,
                                                                         monkeypatch):
    """Accounting and interventions belong to the run, and the run is durable.

    The unreconciled cost is deliberately written as an unknown-cost call as well:
    an interrupted call that reads as free after a restart is the failure this
    repository has seen before, so the known total must stay honest about it.
    """
    store, svc = _service(imported['tmp_path'], monkeypatch)
    eid = imported['execution_id']
    store.append(eid, 'usage.recorded', {'profile': 'standard', 'call_id': 'c1',
                                         'cost_usd': 0.42})
    store.append(eid, 'usage.recorded', {'profile': 'standard', 'call_id': 'c2',
                                         'cost_usd': None, 'interrupted': True})
    store.append(eid, 'followup.pending', {'id': 'f1', 'content': '补充：只改导出',
                                           'actor': 'operator'})
    svc.close()

    store, svc = _service(imported['tmp_path'], monkeypatch)
    svc.recover()
    tasks = MaintenanceTasks(
        store,
        execution=WebuddyExecution(store, dispatch=lambda rid: pytest.fail('不应派发'),
                                   cost=lambda rid: svc._usage(rid)['known_cost_usd']),
        repository=WebuddyRepository(store), identity=WebuddyIdentity())
    view = tasks.get(imported['task_id'], actor=ACTOR)
    assert view['cost_usd'] == pytest.approx(0.42), '重启后已记账的费用不能丢'
    assert svc._usage(eid)['unknown_cost_calls'] == 1, '中断未对账的调用不能被清零成免费'
    supplements = [event for event in tasks.events(imported['task_id'], actor=ACTOR)
                   if event['kind'] == 'followup.pending']
    assert [event['payload']['content'] for event in supplements] == ['补充：只改导出']


def test_a_receipt_written_before_the_restart_is_readable_after_it(imported,
                                                                  monkeypatch):
    """Receipts are module-owned rows with an append-only trigger, not memory."""
    store, _ = _service(imported['tmp_path'], monkeypatch)
    tasks = _tasks(store, dispatch=lambda rid: None)
    receipt = {'schema': 'webuddy.maintenance.receipt/1', 'task_id': imported['task_id'],
               'revision': 1, 'delivery': {'commit': 'x' * 40}}
    tasks.records.put_receipt(imported['task_id'], 1, receipt)

    store2, svc2 = _service(imported['tmp_path'], monkeypatch)
    svc2.recover()
    tasks2 = _tasks(store2, dispatch=lambda rid: pytest.fail('不应派发'))
    view = tasks2.get(imported['task_id'], actor=ACTOR)
    assert [r['delivery']['commit'] for r in view['receipts']] == ['x' * 40]
