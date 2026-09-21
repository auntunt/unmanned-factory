"""Registration and dispatch are two writes; a crash between them stays recoverable.

Two defects sat on either side of the same boundary, and both are checked here
against the real ``Store``, the real ``create_run`` dedup and the real
``DurableQueue`` -- a counting fake port can show that a retry binds *an*
execution, but not that it binds *the one that already exists* and does not
enqueue it twice.

* Opening a client over control.db is not a restart.  The maintenance-job sweep
  used to live in ``Service.__init__``, so the standalone CLI listing tasks
  retired jobs the running service was still working on.  The sweep still
  happens -- on the paths that really are a takeover, which is asserted here in
  both directions so "fixed" cannot mean "deleted".

* ``create`` claimed the idempotency key, then submitted.  A failure in between
  burned the key and left a task permanently in ``received`` with no execution
  to resume, cancel or reconcile.  Three windows can be interrupted -- no
  execution yet, execution built but never queued, queued but never linked --
  and all three must converge on one execution and one queue entry.

No provider is ever called: dispatch here is the durable enqueue and nothing
claims the queue.
"""
from __future__ import annotations

import subprocess

import pytest

from factory.control.app import create_app
from factory.control.autonomy import DurableQueue
from factory.control.issue_maintenance import MaintenanceTasks
from factory.control.issue_maintenance_webuddy import (
    WebuddyExecution, WebuddyIdentity, WebuddyRepository)
from factory.control.service import Service
from factory.control.store import Store

ACTOR = {'id': 5, 'username': 'operator'}
LIVE_JOB = ("INSERT INTO maintenance_jobs VALUES"
            "('live-job','conversation',1,'running',NULL,NULL,'before','before')")


def _profiles():
    return {role: {'provider': 'codex', 'model': 'test'}
            for role in ('planner', 'cheap', 'standard', 'strong')}


class _NeverRuns:
    def run(self, *a, **kw):  # pragma: no cover - reaching this is the bug
        raise AssertionError('这些用例不应触发任何模型调用')


def _service(store, monkeypatch=None):
    svc = Service(store, runner=_NeverRuns(), profiles=_profiles())
    if monkeypatch is not None:
        monkeypatch.setattr(svc, '_ensure_scheduler', lambda: None)
    return svc


def _repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.email', 'f@l'],
                 ['config', 'user.name', 'F']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'README.md').write_text('legacy\n')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=root, check=True,
                   capture_output=True)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    return root, base


def _request(base, project_id, **over):
    return {'issue': {'source': 'manual-synthetic', 'external_id': '9',
                      'version': '1', 'title': '派发中断后要能续上',
                      'body': '合成用例：登记成功、派发失败。'},
            'project_id': project_id, 'repository': 'synthetic/dispatch',
            'base_sha': base, 'base_branch_label': 'main',
            'expected_behaviour': '重试后仍只有一次执行',
            'delivery_goal': '补丁与回执',
            'agreement': {'revision': 'v4', 'skill_version': 'issue-maintenance@2'},
            'idempotency_key': 'dispatch-window-1', 'synthetic': True, **over}


# ---------------------------------------------------------------------------
# R1: a client is not a restart, and a restart is still a restart
# ---------------------------------------------------------------------------
def test_opening_a_client_over_the_database_does_not_retire_live_jobs(tmp_path):
    store = Store(tmp_path / 'control.db')
    first = _service(store)
    try:
        with store.connect() as db:
            db.execute(LIVE_JOB)
        reader = Service(store, runner=_NeverRuns(), profiles=_profiles())
        try:
            assert reader.maintenance_status('live-job')['status'] == 'running', (
                '构造一个客户端不是重启，不能把别的进程正在跑的作业判死')
        finally:
            reader.close()
    finally:
        first.close()


def test_service_startup_recovery_still_retires_unacknowledged_jobs(tmp_path, monkeypatch):
    """The sweep was moved, not removed: ``recover()`` is the real takeover path."""
    store = Store(tmp_path / 'control.db')
    svc = _service(store, monkeypatch)
    try:
        # After construction, so the row is one this process found rather than
        # one it created: the table itself is part of opening a client.
        with store.connect() as db:
            db.execute(LIVE_JOB)
        assert svc.maintenance_status('live-job')['status'] == 'running'
        svc.recover()
        assert svc.maintenance_status('live-job')['status'] == 'interrupted', (
            '真正接管的进程仍必须把没有回执的作业对齐到明确终态')
        assert svc.maintenance_status('live-job')['error']
    finally:
        svc.close()


def test_building_the_web_app_retires_jobs_before_pack_reconciliation(tmp_path):
    """Ordering matters: ``pack_router`` reconciles pack tasks while it is built.

    It reads each task's maintenance job state, so if the sweep ran later (in
    ``lifespan``) a pack task whose job died with the last process would stay
    "处理中" forever.  Asserting on app construction is what pins the sweep ahead
    of the router rather than merely somewhere in startup.
    """
    data = tmp_path / 'data'
    store = Store(data / 'control.db')
    svc = _service(store)
    try:
        with store.connect() as db:
            db.execute(LIVE_JOB)
        create_app(data_dir=data, workspace_root=tmp_path / 'repos',
                   public_origin='http://testserver', service=svc,
                   webhook_secret='test-webhook-secret')
        assert svc.maintenance_status('live-job')['status'] == 'interrupted', (
            '构建应用就是一次服务启动，要在 pack 路由对账之前完成清算')
    finally:
        svc.close()


def test_a_start_that_loses_the_writer_lock_recovers_nothing(tmp_path):
    """The second service is a contender, not a successor.

    Moving the sweep out of ``Service.__init__`` into ``create_app`` only moved
    the violation if building an app is enough to perform it: a second web
    process racing to start would still retire the live process's jobs. Recovery
    is a write reserved for whoever holds the durable queue's lock, so the losing
    start has to come away having changed nothing.
    """
    store = Store(tmp_path / 'control.db')
    active = _service(store)
    contender = None
    try:
        active.recover()  # this process owns the database
        with store.connect() as db:
            db.execute(LIVE_JOB)
        contender = Service(store, runner=_NeverRuns(), profiles=_profiles())
        create_app(data_dir=tmp_path / 'data', workspace_root=tmp_path / 'repos',
                   public_origin='http://testserver', service=contender,
                   webhook_secret='test-webhook-secret')
        assert active.maintenance_status('live-job')['status'] == 'running', (
            '没有拿到写入锁的启动方不能执行重启恢复')
        assert contender.queue.handle is None, '竞争失败的一方不应持有锁'
    finally:
        if contender is not None:
            contender.close()
        active.close()


def test_the_losing_start_is_told_another_factory_owns_the_database(tmp_path):
    """Recovering nothing must not mean starting quietly as a second writer.

    Skipping the sweep is only half the answer: a contender that then served
    requests would be the second coordinator the lock exists to prevent. The
    refusal is the one the queue already raises, surfaced where the application
    really starts.
    """
    from fastapi.testclient import TestClient
    from factory.control.store import Conflict

    store = Store(tmp_path / 'control.db')
    active = _service(store)
    contender = None
    try:
        active.recover()
        contender = Service(store, runner=_NeverRuns(), profiles=_profiles())
        app = create_app(data_dir=tmp_path / 'data', workspace_root=tmp_path / 'repos',
                         public_origin='http://testserver', service=contender,
                         webhook_secret='test-webhook-secret')
        with pytest.raises(Conflict) as refused:
            with TestClient(app):
                pass  # pragma: no cover - entering means the race was allowed
        assert '已有工厂进程' in str(refused.value), str(refused.value)
    finally:
        if contender is not None:
            contender.close()
        active.close()


def test_the_owner_can_acquire_again_in_lifespan(tmp_path):
    """The claim is idempotent, or the winner would refuse its own startup."""
    store = Store(tmp_path / 'control.db')
    svc = _service(store)
    try:
        with store.connect() as db:
            db.execute(LIVE_JOB)
        create_app(data_dir=tmp_path / 'data', workspace_root=tmp_path / 'repos',
                   public_origin='http://testserver', service=svc,
                   webhook_secret='test-webhook-secret')
        assert svc.queue.handle is not None, '赢家应当持有锁'
        assert svc.maintenance_status('live-job')['status'] == 'interrupted'
        svc.recover()  # lifespan's path, on a lock this service already holds
        assert svc.maintenance_status('live-job')['status'] == 'interrupted'
    finally:
        svc.close()


def test_a_failed_app_build_gives_the_lock_back(tmp_path, monkeypatch):
    """A claim that never becomes a running app must not outlive the attempt.

    Otherwise the failed start leaves the database owned by a process that is not
    serving, and the next start -- the one that would have worked -- is refused.
    """
    import factory.control.pack_routes as pack_routes

    store = Store(tmp_path / 'control.db')
    svc = _service(store)
    try:
        monkeypatch.setattr(pack_routes, 'router', _explodes)
        with pytest.raises(RuntimeError):
            create_app(data_dir=tmp_path / 'data', workspace_root=tmp_path / 'repos',
                       public_origin='http://testserver', service=svc,
                       webhook_secret='test-webhook-secret')
        assert svc.queue.handle is None, '构建失败后必须把锁还回去'
        successor = Service(store, runner=_NeverRuns(), profiles=_profiles())
        try:
            successor.recover()  # refuses if the failed start still held the lock
        finally:
            successor.close()
    finally:
        svc.close()


def _explodes(*args, **kwargs):
    raise RuntimeError('构建路由时失败')


# ---------------------------------------------------------------------------
# R2: the three interrupted windows between claim and dispatch
# ---------------------------------------------------------------------------
@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Real store, real create_run dedup, real durable queue, counted dispatch."""
    root, base = _repo(tmp_path)
    store = Store(tmp_path / 'control.db')
    svc = _service(store, monkeypatch)
    project = store.add_project({
        'name': '合成派发仓库', 'repository': 'synthetic/dispatch',
        'workspace': str(root), 'base_branch': 'main', 'budget_usd': 5.0,
        'checks': {}, 'max_tasks': 1})
    queue = DurableQueue(store)
    dispatched: list[str] = []

    def dispatch(rid):
        dispatched.append(rid)
        queue.enqueue(rid, 'plan')

    tasks = MaintenanceTasks(
        store,
        execution=WebuddyExecution(store, dispatch=dispatch, cost=lambda eid: 0.0),
        repository=WebuddyRepository(store),
        identity=WebuddyIdentity())
    try:
        yield {'store': store, 'tasks': tasks, 'queue': queue,
               'dispatched': dispatched, 'request': _request(base, project['id'])}
    finally:
        svc.close()


def _maintenance_runs(store):
    return [run for run in store.all_runs()
            if (run.get('source') or {}).get('type') == 'issue_maintenance']


def test_retry_after_the_execution_was_never_built_dispatches_exactly_one(bench, monkeypatch):
    """Window 1: nothing was created, so the retry has to create it."""
    original = bench['store'].create_run
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('登记执行前中断')
        return original(*args, **kwargs)

    monkeypatch.setattr(bench['store'], 'create_run', fail_once)
    with pytest.raises(RuntimeError):
        bench['tasks'].create(bench['request'], actor=ACTOR)

    view = bench['tasks'].create(bench['request'], actor=ACTOR)
    assert view['execution_id'], '重试必须把保留下来的任务接上执行，而不是永远孤儿'
    runs = _maintenance_runs(bench['store'])
    assert len(runs) == 1, f'只应存在一次执行，实际 {len(runs)}'
    assert view['execution_id'] == runs[0]['id']
    assert bench['dispatched'] == [runs[0]['id']]


def test_retry_after_dispatch_failed_queues_the_same_execution(bench, monkeypatch):
    """Window 2: the run exists but never reached the queue -- the silent orphan.

    ``create_run`` dedups on the task id, so a naive retry gets the run back with
    ``created`` False and skips dispatch; the execution would then sit in
    ``received`` with nothing coming for it.
    """
    dispatched = bench['dispatched']
    queue = bench['queue']
    real_enqueue = queue.enqueue
    attempts = []

    def enqueue_fails_once(rid, phase, **kwargs):
        attempts.append(rid)
        if len(attempts) == 1:
            raise RuntimeError('执行已建立，入队时中断')
        return real_enqueue(rid, phase, **kwargs)

    monkeypatch.setattr(queue, 'enqueue', enqueue_fails_once)
    with pytest.raises(RuntimeError):
        bench['tasks'].create(bench['request'], actor=ACTOR)

    runs = _maintenance_runs(bench['store'])
    assert len(runs) == 1, '第一次已经建立了执行'
    orphan = runs[0]['id']
    assert queue.jobs(orphan) == [], '前提：这次执行没有进入队列'

    view = bench['tasks'].create(bench['request'], actor=ACTOR)
    assert view['execution_id'] == orphan, '重试必须接上同一次执行，不能再买一次'
    assert len(_maintenance_runs(bench['store'])) == 1
    jobs = queue.jobs(orphan)
    assert len(jobs) == 1 and jobs[0]['generation'] == 1, f'队列条目：{jobs}'
    assert dispatched == [orphan, orphan]


def test_retry_after_link_failed_reuses_execution_without_queueing_twice(bench, monkeypatch):
    """Window 3: dispatched already; the retry must reconcile, not re-dispatch."""
    tasks = bench['tasks']
    real_link = tasks.records.link
    calls = []

    def link_fails_once(task_id, changes):
        calls.append(task_id)
        if len(calls) == 1:
            raise RuntimeError('执行已入队，回填引用时中断')
        return real_link(task_id, changes)

    monkeypatch.setattr(tasks.records, 'link', link_fails_once)
    with pytest.raises(RuntimeError):
        tasks.create(bench['request'], actor=ACTOR)

    runs = _maintenance_runs(bench['store'])
    assert len(runs) == 1
    execution_id = runs[0]['id']
    assert bench['dispatched'] == [execution_id], '前提：这次执行已经派发过'

    view = tasks.create(bench['request'], actor=ACTOR)
    assert view['execution_id'] == execution_id
    assert bench['dispatched'] == [execution_id], '已派发的执行不能被重试再派发一次'
    jobs = bench['queue'].jobs(execution_id)
    assert len(jobs) == 1 and jobs[0]['generation'] == 1, f'队列条目：{jobs}'


def test_repeat_import_of_a_bound_task_never_dispatches_again(bench):
    """The ordinary duplicate import: unchanged behaviour, asserted by count."""
    first = bench['tasks'].create(bench['request'], actor=ACTOR)
    again = bench['tasks'].create(bench['request'], actor=ACTOR)
    assert again['task_id'] == first['task_id']
    assert again['execution_id'] == first['execution_id']
    assert bench['dispatched'] == [first['execution_id']]
    assert len(_maintenance_runs(bench['store'])) == 1
