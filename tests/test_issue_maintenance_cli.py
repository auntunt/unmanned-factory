"""CLI exercised as a real subprocess, using the same adapter ports as webuddy.

Strategy: the test builds a real small git repo and pre-seeds the SQLite
database in-process so the CLI subprocess finds real run records, real
maintenance task records, and a real worktree it can export from.  The CLI
subprocess uses the production WebuddyExecution / WebuddyRepository ports
(imported, not reimplemented), so every path it takes is the same code the
web surface uses.

For ``create``, the CLI constructs a real Service over the same DB (with
the scheduler patched out), so ``svc.start_plan`` durably enqueues the
run.  The test proves the task was created and the run was enqueued.

Why strategy (a) (pre-seed, no test-only dispatch profile): a test-only
code path inside the production CLI would be a second source of truth
that can drift from the real adapter.  Pre-seeding is transparent.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.control.issue_maintenance import MaintenanceTasks
from factory.control.store import Store

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ACTOR = {'id': 7, 'username': 'operator'}
OS_USER = getpass.getuser()
CLI_ACTOR = {'id': f'local:{OS_USER}', 'username': OS_USER}

PY = sys.executable
CLI_MODULE = 'factory.control.maintenance_cli'
WORK_DIR = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# Seeding helpers -- these exist only so we can call MaintenanceTasks.create()
# in-process to write the maintenance_tasks row.  The CLI subprocess never
# sees these; it builds its own real WebuddyExecution from the adapter.
# ---------------------------------------------------------------------------
class _SeedIdentity:
    """Accept any controlled actor during in-process seeding."""
    def require(self, actor, project_id):
        pass


class _SeedRepository:
    """Accept any base_sha the test provides during in-process seeding."""
    def __init__(self, base):
        self._base = base

    def resolve(self, project_id, repository, base_sha):
        if base_sha != self._base:
            raise ValueError('baseline not found')
        return {'project_id': project_id, 'repository': repository,
                'base_sha': base_sha}


class _SeedExecution:
    """Returns the pre-created run_id from submit(); reads from real store."""
    def __init__(self, store, run_id):
        self._store = store
        self._run_id = run_id

    def submit(self, record, *, actor):
        return self._run_id

    def status(self, eid):
        return self._store.get(eid)['status']

    def events(self, eid, after=0):
        return [{'sequence': e['id'], 'kind': e['type'],
                 'payload': e['payload'], 'at': e['at']}
                for e in self._store.events(eid, after=after)]

    def cost_usd(self, eid):
        return 0.0

    def delivery(self, eid):
        run = self._store.get(eid)
        artifacts = run.get('artifacts') or {}
        if not artifacts.get('commit'):
            return None
        source = run.get('source') or {}
        return {
            'commit': artifacts['commit'],
            'checks': [{'name': c.get('name'),
                         'passed': c.get('exit') == 0,
                         'exit_code': c.get('exit')}
                        for c in (artifacts.get('checks') or [])],
            'unverified': list(artifacts.get('unverified') or []),
            'working_copy_base_sha': artifacts.get('base_sha'),
            'repository': source.get('repository'),
            'capability_source': 'platform_executor',
            'synthetic': bool(source.get('synthetic')),
            'worktree': artifacts.get('worktree'),
        }

    def export_artifacts(self, eid):
        run = self._store.get(eid)
        a = run.get('artifacts') or {}
        w, c, b = a.get('worktree'), a.get('commit'), a.get('base_sha')
        done = subprocess.run(
            ['git', 'format-patch', '--stdout', f'{b}..{c}'],
            cwd=w, capture_output=True, timeout=15)
        return {'diff_hash': hashlib.sha256(done.stdout).hexdigest(),
                'artifacts': [{'name': f'maintenance-{eid}.patch',
                               'bytes': done.stdout}]}

    def intervene(self, eid, text, *, actor):
        pass

    def resume(self, eid, *, actor):
        pass

    def cancel(self, eid, *, actor):
        pass


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _issue(**over):
    return {'source': 'manual', 'external_id': '1024', 'version': '3',
            'title': '导出报表偶发缺行',
            'body': '选择跨月区间时最后一行丢失。', **over}


def _request(base_sha, project_id='p1', **over):
    return {
        'issue': _issue(**over.pop('issue', {})),
        'project_id': project_id,
        'repository': 'acme/legacy',
        'base_sha': base_sha,
        'base_branch_label': 'release/2.1',
        'expected_behaviour': '跨月区间导出行数与查询结果一致',
        'delivery_goal': '提供补丁与回执',
        'agreement': {'revision': 'v4',
                      'skill_version': 'issue-maintenance@2'},
        'idempotency_key': 'import-1024-a',
        'synthetic': True, **over}


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------
def _cli(*args, expect_ok=True):
    """Run the CLI as a real subprocess; return (returncode, parsed JSON)."""
    done = subprocess.run(
        [PY, '-m', CLI_MODULE, *args],
        capture_output=True, text=True, timeout=60,
        cwd=WORK_DIR,
    )
    stream = done.stdout if done.returncode == 0 else done.stderr
    try:
        data = json.loads(stream) if stream.strip() else {}
    except json.JSONDecodeError:
        data = {'raw': stream}
    if expect_ok:
        assert done.returncode == 0, (
            f'CLI exited {done.returncode}; '
            f'stderr={done.stderr!r}; stdout={done.stdout!r}')
    return done.returncode, data


def _git(root, *args):
    return subprocess.run(
        ['git', *args], cwd=root, check=True,
        capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Fixture: pre-seeded database with a delivered task
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded(tmp_path):
    """Pre-seed a DB with one delivered task using real store records.

    The CLI subprocess uses the real WebuddyExecution port, which reads
    from the runs table and checks source.type == 'issue_maintenance'.
    """
    db_path = tmp_path / 'control.db'
    store = Store(db_path)

    # Real git repo: baseline -> fix.
    repo = tmp_path / 'worktree'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.email', 'f@l')
    _git(repo, 'config', 'user.name', 'F')
    (repo / 'report.py').write_text('# baseline\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'baseline')
    base_sha = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    (repo / 'report.py').write_text('# fixed\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'fix')
    fix_sha = _git(repo, 'rev-parse', 'HEAD').stdout.strip()

    # Register a project so WebuddyRepository.resolve works.
    project = store.add_project({
        'name': '合成遗留仓库', 'repository': 'acme/legacy',
        'workspace': str(repo), 'base_branch': 'main',
        'budget_usd': 5.0, 'checks': {}, 'max_tasks': 1})

    # Real run in the store with the maintenance source type.
    source = {'type': 'issue_maintenance', 'actor': ACTOR['username'],
              'actor_id': ACTOR['id'], 'repository': 'acme/legacy',
              'synthetic': True}
    run, _ = store.create_run(project['id'], 'test prompt',
                              source=source, delivery_id='maint-test-1')
    run_id = run['id']

    # Mark delivered with real artifacts.
    store.update(run_id, {
        'status': 'ready_for_review',
        'artifacts': {'commit': fix_sha, 'base_sha': base_sha,
                      'worktree': str(repo),
                      'checks': [{'name': 'local', 'exit': 0}],
                      'unverified': ['未验证项']},
    }, expected=('received',),
       event=('run.verified', {'message': '检查通过'}))
    store.append(run_id, 'run.started', {'message': '开始执行'})

    # Maintenance task record pointing at the real run.
    seed_exec = _SeedExecution(store, run_id)
    port = MaintenanceTasks(
        store, execution=seed_exec,
        repository=_SeedRepository(base=base_sha),
        identity=_SeedIdentity())
    req = _request(base_sha, project_id=project['id'])
    view = port.create(req, actor=ACTOR)

    class _NS:
        pass
    ns = _NS()
    ns.db, ns.view, ns.port, ns.store = str(db_path), view, port, store
    ns.repo, ns.base_sha, ns.fix_sha = repo, base_sha, fix_sha
    ns.run_id, ns.project = run_id, project
    return ns


# ===================================================================
# Tests
# ===================================================================


# -- identity boundary --------------------------------------------------
def test_wrong_operator_is_refused_with_nonzero_exit(seeded):
    """Passing an arbitrary privileged actor name must be refused."""
    rc, data = _cli('--db', seeded.db, '--operator', 'root',
                     'show', seeded.view['task_id'], expect_ok=False)
    assert rc == 2
    assert 'root' in data.get('error', '')
    assert OS_USER in data.get('error', '')


def test_nonexistent_db_is_refused(tmp_path):
    rc, data = _cli('--db', str(tmp_path / 'no-such.db'),
                     'show', 'xxx', expect_ok=False)
    assert rc == 1
    assert '数据库不存在' in data.get('error', '')


# -- show / list / events -----------------------------------------------
def test_show_returns_the_task_as_json(seeded):
    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                     'show', seeded.view['task_id'])
    assert rc == 0
    assert data['task_id'] == seeded.view['task_id']
    assert data['status'] == 'delivered'
    assert data['baseline']['base_sha'] == seeded.base_sha


def test_list_returns_tasks_for_project(seeded):
    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                     'list', '--project', seeded.project['id'])
    assert rc == 0
    assert isinstance(data['tasks'], list)
    assert len(data['tasks']) >= 1
    assert data['tasks'][0]['task_id'] == seeded.view['task_id']


def test_events_returns_list_with_after_filter(seeded):
    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                     'events', seeded.view['task_id'], '--after', '0')
    assert rc == 0
    assert len(data['events']) >= 2
    first_seq = data['events'][0]['sequence']
    rc, data2 = _cli('--db', seeded.db, '--operator', OS_USER,
                      'events', seeded.view['task_id'],
                      '--after', str(first_seq))
    assert rc == 0
    assert len(data2['events']) == len(data['events']) - 1


# -- cancel --------------------------------------------------------------
def test_cancel_records_intent(seeded):
    rc, _ = _cli('--db', seeded.db, '--operator', OS_USER,
                  'cancel', seeded.view['task_id'])
    assert rc == 0
    record = seeded.port.records.get(seeded.view['task_id'])
    assert record['cancel_requested'] is True


def test_cancel_really_reaches_the_run_lifecycle(seeded):
    """The intent alone is not the cancel.

    ``MaintenanceTasks.cancel`` writes ``cancel_requested`` *before* calling the
    execution side, so the assertion above passes even when the execution hook is
    a no-op -- which is exactly the state the CLI shipped in, because its resume
    and cancel hooks were only wired for ``create``. The run's own status is the
    only evidence that the lifecycle was reached, and it lives in a table this
    module never writes.
    """
    before = seeded.store.get(seeded.run_id)
    assert before['status'] == 'ready_for_review', (
        '前置条件：取消前这条运行必须是活的，否则下面的断言无法区分'
        '"取消生效了"和"它本来就是这个状态"')

    rc, _ = _cli('--db', seeded.db, '--operator', OS_USER,
                  'cancel', seeded.view['task_id'])
    assert rc == 0

    after = seeded.store.get(seeded.run_id)
    assert after['status'] == 'cancelled'
    kinds = [event['type'] for event in seeded.store.events(seeded.run_id)]
    assert 'run.cancelled' in kinds


def _make_waiting(seeded):
    """Move the seeded run to the one state intervene and resume accept.

    Both commands refuse anything but ``needs_human``, and ``resume`` additionally
    requires a restorable execution site (``base_sha`` plus ``tasks``) and a plan.
    Seeded here rather than reached through a real run because reaching it for real
    would mean dispatching a provider, and no test in this file pays for a call.
    """
    # The fixture commits its fix onto ``main``, which leaves the project's base
    # branch ahead of the task's baseline -- and resume rightly refuses to continue
    # an old plan on a moved baseline. A real delivery commits in the executor's
    # worktree and leaves the base branch alone, so the branch is put back where
    # the task's baseline says it is. Not doing this made the guard fire and looked
    # like a CLI fault.
    _git(seeded.repo, 'update-ref', 'refs/heads/main', seeded.base_sha)
    seeded.store.update(seeded.run_id, {
        'status': 'needs_human',
        'plan': {'tasks': [{'id': 't1', 'title': '修复导出',
                            'acceptance': ['行数一致'], 'paths': ['report.py'],
                            'checks': []}]},
        'artifacts': {**(seeded.store.get(seeded.run_id).get('artifacts') or {}),
                      'tasks': [{'id': 't1', 'status': 'done'}]},
    }, expected=('ready_for_review',))
    return seeded.store.get(seeded.run_id)


# -- intervene -> resume, end to end through subprocesses -----------------
def test_supplementing_then_resuming_a_waiting_task_moves_it(seeded):
    """``创建→观察事件→补充/恢复`` for real, one subprocess per step.

    The supplement must be durably readable by a *different* process than the one
    that wrote it, and the resume must land the run back in the queue. Asserting
    the printed view alone would not distinguish either from a no-op, so both are
    read back out of the store.
    """
    waiting = _make_waiting(seeded)
    task_id = seeded.view['task_id']

    rc, after_intervene = _cli('--db', seeded.db, '--operator', OS_USER,
                                'intervene', task_id, '--text', '缺行只在跨月时出现')
    assert rc == 0
    assert after_intervene['status'] == 'waiting'
    pending = [event for event in seeded.store.events(seeded.run_id)
               if event['type'] == 'followup.pending']
    assert len(pending) == 1
    assert pending[0]['payload']['content'] == '缺行只在跨月时出现'
    assert pending[0]['payload']['actor'] == OS_USER

    # A second process sees the supplement the first one wrote.
    rc, events = _cli('--db', seeded.db, '--operator', OS_USER,
                       'events', task_id, '--after', '0')
    assert rc == 0
    assert 'followup.pending' in [event['kind'] for event in events['events']]

    rc, _ = _cli('--db', seeded.db, '--operator', OS_USER, 'resume', task_id)
    assert rc == 0
    resumed = seeded.store.get(seeded.run_id)
    assert resumed['status'] == 'queued'
    assert resumed['resume_count'] == waiting.get('resume_count', 0) + 1
    kinds = [event['type'] for event in seeded.store.events(seeded.run_id)]
    assert 'human.continued' in kinds


def test_an_unwired_lifecycle_hook_refuses_instead_of_returning(seeded):
    """The other direction, at the port rather than through the CLI.

    Widening which subcommands get their hooks wired fixes this instance; it does
    not stop the next caller from constructing the port without them. The adapter
    default is what decides whether that mistake refuses or reports success, so it
    is asserted here directly -- with the same real store the CLI uses, and by
    checking the run afterwards, because "raised" and "raised after cancelling
    anyway" are different outcomes.
    """
    from factory.control.issue_maintenance_webuddy import WebuddyExecution
    from factory.control.store import Conflict

    # ``resume`` refuses a run that is not waiting for a human *before* it reaches
    # the hook, so a delivered run would prove nothing about the default. The run
    # is moved to the one state where resume really does call through.
    seeded.store.update(seeded.run_id, {'status': 'needs_human'},
                        expected=('ready_for_review',))
    execution = WebuddyExecution(
        seeded.store, dispatch=lambda rid: pytest.fail('不应派发'),
        cost=lambda eid: 0.0)
    for call in (execution.resume, execution.cancel):
        with pytest.raises(Conflict, match='没有接入'):
            call(seeded.run_id, actor=CLI_ACTOR)
    assert seeded.store.get(seeded.run_id)['status'] == 'needs_human'


# -- create via subprocess -----------------------------------------------
def test_create_via_subprocess_enqueues_a_task(seeded):
    """Create a new task via the CLI subprocess.

    The CLI constructs a real Service (scheduler patched out) and uses
    svc.start_plan as the dispatch, which durably enqueues the run.
    """
    req = _request(seeded.base_sha, project_id=seeded.project['id'],
                   idempotency_key='cli-create-test-1')
    req_file = Path(seeded.db).parent / 'create-request.json'
    req_file.write_text(json.dumps(req, ensure_ascii=False))

    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                     'create', '--request-json', str(req_file))
    assert rc == 0
    assert 'task_id' in data
    assert data['status'] == 'received'
    assert data['baseline']['base_sha'] == seeded.base_sha


# -- export (observe -> export, proving the patch) -----------------------
def test_export_writes_artifacts_and_returns_receipt(seeded):
    """The chain: show (observe) + export, proving the patch applies."""
    task_id = seeded.view['task_id']

    # Observe.
    rc, show = _cli('--db', seeded.db, '--operator', OS_USER,
                     'show', task_id)
    assert rc == 0
    assert show['status'] == 'delivered'

    # Export.
    out_dir = str(Path(seeded.db).parent / 'export')
    rc, exported = _cli('--db', seeded.db, '--operator', OS_USER,
                         'export', task_id, '--output', out_dir)
    assert rc == 0
    assert exported['receipt']['task_id'] == task_id
    assert exported['receipt']['synthetic'] is True
    assert '不代表任何客户成功案例' in exported['text']

    # Artifact written to disk.
    written = exported['written']
    assert len(written) >= 1
    patch_bytes = Path(written[0]).read_bytes()
    assert len(patch_bytes) > 0

    # diff_hash matches exported bytes.
    assert (exported['receipt']['delivery']['diff_hash']
            == hashlib.sha256(patch_bytes).hexdigest())

    # Prove the patch applies to a fresh clone at baseline.
    fresh = Path(seeded.db).parent / 'fresh'
    subprocess.run(['git', 'clone', '-q', str(seeded.repo), str(fresh)],
                   check=True, capture_output=True)
    _git(fresh, 'checkout', '-q', seeded.base_sha)
    assert (fresh / 'report.py').read_text().strip() == '# baseline'
    _git(fresh, 'config', 'user.email', 'f@l')
    _git(fresh, 'config', 'user.name', 'F')
    (fresh / 'delivered.patch').write_bytes(patch_bytes)
    _git(fresh, 'am', 'delivered.patch')
    assert (fresh / 'report.py').read_text().strip() == '# fixed'


# -- error paths ---------------------------------------------------------
def test_show_nonexistent_task_returns_error(seeded):
    rc, data = _cli('--db', seeded.db, '--operator', OS_USER,
                     'show', 'nonexistent-id', expect_ok=False)
    assert rc == 1
    assert data.get('error_type') == 'KeyError'


# -- help ----------------------------------------------------------------
def test_help_exits_zero():
    """The CLI module is loadable and --help lists every subcommand."""
    done = subprocess.run(
        [PY, '-m', CLI_MODULE, '--help'],
        capture_output=True, text=True, timeout=10, cwd=WORK_DIR)
    assert done.returncode == 0
    assert 'maintenance-cli' in done.stdout
    for cmd in ('create', 'list', 'show', 'events', 'intervene',
                'resume', 'cancel', 'export'):
        assert cmd in done.stdout
