"""No second writer against a site a previous execution still owns.

Two mechanisms, tested against real processes and a real database rather than
against a story about them:

`control_jobs.generation` already existed and `enqueue` already bumped it, but
`claim`/`finish` did not carry it, so a slow worker's `finally` retired whichever
round happened to be running -- including a newer one it never ran.

Whether the previous provider worker exited was not established at all. It cannot be
read from a pid, a dropped browser connection, or a durable status row that a crash
is exactly what leaves stale. The worker holds an exclusive OS lock for its process
lifetime instead; the kernel drops it on death, however the process died.

No paid model call happens here: the subprocesses are plain Python holding a lock.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory.control import provider_activity
from factory.control.autonomy import DurableQueue
from factory.control.store import Store


def _holder(path, seconds='30'):
    """A real separate process that holds the activity lock and nothing else."""
    code = ('import sys, time\n'
            'from factory.control import provider_activity\n'
            'h = provider_activity.hold(sys.argv[1])\n'
            'sys.stdout.write("held\\n"); sys.stdout.flush()\n'
            'time.sleep(float(sys.argv[2]))\n')
    proc = subprocess.Popen([sys.executable, '-c', code, str(path), seconds],
                            stdout=subprocess.PIPE, text=True,
                            cwd=str(Path(__file__).resolve().parent.parent))
    assert proc.stdout.readline().strip() == 'held', 'holder did not take the lock'
    return proc


def test_a_live_provider_worker_is_seen_and_its_exit_is_seen(tmp_path):
    """The predicate answers from the lock, not from the process table.

    Both directions matter: a live writer must read as active, and a writer that
    died without cleaning up anything must read as gone. A killed process leaves
    its lock file on disk, so file existence alone would report it forever active.
    """
    path = tmp_path / 'provider-activity.lock'
    assert provider_activity.is_active(path) is False  # nothing ever ran here

    proc = _holder(path)
    try:
        assert provider_activity.is_active(path) is True
        assert Path(path).exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Killed, not asked to shut down: no code of ours ran on the way out.
    deadline = time.monotonic() + 5
    while provider_activity.is_active(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert provider_activity.is_active(path) is False
    assert Path(path).exists(), 'the stale file remains; only the lock reports the exit'


def test_recovery_refuses_to_start_a_second_writer_on_a_live_workspace(tmp_path,
                                                                      monkeypatch):
    """`execute_continuous` recovery blocks while the old worker still holds the site.

    Driven through the real recovery validation path, with the paid provider call
    replaced by a runner that fails the test if it is ever reached: the point is that
    recovery stops before dispatching anything.
    """
    from factory.control import continuous
    from factory.control.execution import ExecutionError

    repo = tmp_path / 'repos' / 'sample'
    repo.mkdir(parents=True)
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'T'],
                 ['config', 'user.email', 't@e.com']):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'greeting.txt').write_text('hello')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True,
                   capture_output=True)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    worktree = tmp_path / 'repos' / '.factory-run' / 'coding'
    subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'factory/run', str(worktree),
                    base], cwd=repo, check=True, capture_output=True)
    lock = provider_activity.lock_path_for(worktree)
    proc = _holder(lock)
    try:
        artifacts = {'execution_mode': 'continuous', 'base_sha': base,
                     'worktree': str(worktree), 'branch': 'factory/run',
                     'activity_lock': lock, 'session_id': 's1',
                     'workspace_guard': continuous._guard_snapshot(
                         continuous._baseline(worktree)),
                     'session_profile': {'provider': 'codex', 'model': 'test',
                                         'profile': 'standard'},
                     'execution_checks': {'greeting': ['true']},
                     'tasks': [{'id': 'coding', 'status': 'running'}]}
        project = {'id': 'p1', 'workspace': str(repo), 'base_branch': 'main',
                   'checks': {'greeting': ['true']}, 'budget_usd': 10}
        plan = {'tasks': [{'id': 'coding', 'title': 'a', 'prompt': 'x',
                           'checks': ['greeting'], 'acceptance': ['ok']}]}
        import threading
        with pytest.raises(ExecutionError) as raised:
            continuous.execute_continuous(
                run_id='run', plan=plan, project=project,
                profiles={'standard': {'provider': 'codex', 'model': 'test'}},
                runner=type('R', (), {'run': lambda *a, **k: pytest.fail(
                    'recovery dispatched a second writer')})(),
                emit=lambda *a, **k: None, cancel=threading.Event(),
                resume_artifacts=artifacts)
        assert raised.value.error_type == 'provider_active'
        assert '仍在写这个工作区' in str(raised.value)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _real_worker_with_descendant(tmp_path, lock, markers):
    """The real `sdk_worker.main`, whose SDK call starts an ordinary local child.

    Only the paid SDK function is replaced. Everything else is production: the
    JSONL request, the activity lock the worker takes, and the process tree the
    launcher created around it. The child is the thing that actually writes, which
    is what makes this different from a lone lock holder.
    """
    ready, go, wrote = markers
    child = ('import sys, time\n'
             'from pathlib import Path\n'
             'ready, go, wrote = map(Path, sys.argv[1:])\n'
             "ready.write_text('ready')\n"
             'while not go.exists(): time.sleep(.01)\n'
             "wrote.write_text('the descendant wrote after its wrapper died')\n"
             'time.sleep(30)\n')
    parent = ('import subprocess, sys, time\n'
              'from factory.control import sdk_worker\n'
              'def fake_sdk(request, emit):\n'
              '    subprocess.Popen([sys.executable, "-c", sys.argv[1], *sys.argv[2:]],\n'
              '                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n'
              '                     stderr=subprocess.DEVNULL)\n'
              '    time.sleep(30)\n'
              'sdk_worker._run_claude = fake_sdk\n'
              'raise SystemExit(sdk_worker.main())\n')
    proc = subprocess.Popen(
        [sys.executable, '-c', parent, child, str(ready), str(go), str(wrote)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=str(Path(__file__).resolve().parent.parent))
    request = {'provider': 'claude', 'model': 'fake', 'prompt': 'local child',
               'workspace': str(tmp_path), 'activity_lock': str(lock)}
    proc.stdin.write((json.dumps(request) + '\n').encode())
    proc.stdin.flush()
    proc.stdin.close()
    deadline = time.monotonic() + 10
    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists(), 'the writer descendant never started'
    return proc


def _site(tmp_path):
    """A real repo plus the coding worktree a continuous run recovers into."""
    repo = tmp_path / 'repos' / 'sample'
    repo.mkdir(parents=True)
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', 'T'],
                 ['config', 'user.email', 't@e.com']):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'greeting.txt').write_text('hello')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True,
                   capture_output=True)
    base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    worktree = tmp_path / 'repos' / '.factory-run' / 'coding'
    subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'factory/run', str(worktree),
                    base], cwd=repo, check=True, capture_output=True)
    return repo, worktree, base


def _attempt_recovery(repo, worktree, base):
    """Drive the real recovery validation; the runner fails if anything dispatches."""
    import threading
    from factory.control import continuous
    artifacts = {'execution_mode': 'continuous', 'base_sha': base,
                 'worktree': str(worktree), 'branch': 'factory/run',
                 'activity_lock': provider_activity.lock_path_for(worktree),
                 'session_id': 's1',
                 'workspace_guard': continuous._guard_snapshot(
                     continuous._baseline(worktree)),
                 'session_profile': {'provider': 'codex', 'model': 'test',
                                     'profile': 'standard'},
                 'execution_checks': {'greeting': ['true']},
                 'tasks': [{'id': 'coding', 'status': 'running'}]}
    project = {'id': 'p1', 'workspace': str(repo), 'base_branch': 'main',
               'checks': {'greeting': ['true']}, 'budget_usd': 10}
    plan = {'tasks': [{'id': 'coding', 'title': 'a', 'prompt': 'x',
                       'checks': ['greeting'], 'acceptance': ['ok']}]}
    return continuous.execute_continuous(
        run_id='run', plan=plan, project=project,
        profiles={'standard': {'provider': 'codex', 'model': 'test'}},
        runner=type('R', (), {'run': lambda *a, **k: pytest.fail(
            'recovery dispatched a second writer')})(),
        emit=lambda *a, **k: None, cancel=threading.Event(),
        resume_artifacts=artifacts)


def test_recovery_still_blocks_when_only_the_dead_wrappers_descendant_writes(tmp_path):
    """The whole chain: real worker, real SDK descendant, real recovery decision.

    This is the case a lock alone cannot answer. The wrapper is SIGKILLed, so its
    lock is gone and nothing in the process table belongs to the coordinator any
    more -- yet the child it started is still writing the worktree. Recovery has
    to refuse, or two writers share the site.
    """
    from factory.control.execution import ExecutionError
    repo, worktree, base = _site(tmp_path)
    lock = Path(provider_activity.lock_path_for(worktree))
    markers = tuple(tmp_path / name for name in ('ready', 'go', 'wrote'))
    proc = _real_worker_with_descendant(worktree, lock, markers)
    try:
        proc.kill()
        proc.wait(timeout=10)
        markers[1].touch()
        deadline = time.monotonic() + 5
        while not markers[2].exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert markers[2].exists(), 'premise: the orphaned descendant really writes'
        with pytest.raises(ExecutionError) as raised:
            _attempt_recovery(repo, worktree, base)
        assert raised.value.error_type == 'provider_active'
        assert '仍在写这个工作区' in str(raised.value)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=10)


def test_a_controlled_shutdown_leaves_the_site_recoverable(tmp_path):
    """A normal end must clear the record, or nothing could ever resume.

    Driven through the real `SDKRunner`, whose worker takes the activity lock in a
    separate process and exits normally. Blocking has to be the answer to an
    unconfirmed exit, not to every exit.
    """
    from factory.control.providers import ProviderRequest, SDKRunner
    repo, worktree, base = _site(tmp_path)
    lock = provider_activity.lock_path_for(worktree)
    worker = tmp_path / 'worker.py'
    worker.write_text(
        'import json, sys\n'
        f'sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n'
        'from factory.control import provider_activity\n'
        'request = json.loads(sys.stdin.readline())\n'
        "handle = provider_activity.hold(request['activity_lock'])\n"
        "print(json.dumps({'type': 'complete', 'payload': {'text': 'done'}}))\n")
    result = SDKRunner(worker_command=[sys.executable, str(worker)]).run(
        ProviderRequest('claude', 'm', 'p', str(worktree), activity_lock=lock),
        lambda *a, **k: None)
    assert result.text == 'done'
    assert provider_activity.registered(lock) is None, 'the record outlived a clean exit'
    assert provider_activity.is_active(lock) is False
    # And recovery proceeds past the liveness gate rather than refusing forever.
    with pytest.raises(Exception) as raised:
        _attempt_recovery(repo, worktree, base)
    assert getattr(raised.value, 'error_type', None) != 'provider_active'


def test_a_killed_wrapper_does_not_report_its_writing_descendant_as_gone(tmp_path):
    """The predicate answers about the writer *tree*, not about the wrapper pid.

    The wrapper holds the lock, so killing it releases the lock while the SDK
    child it started keeps writing the workspace. Reading that free lock as an
    exit is what would let recovery start a second writer alongside a live one.
    """
    lock = Path(provider_activity.lock_path_for(tmp_path / 'coding'))
    markers = tuple(tmp_path / name for name in ('ready', 'go', 'wrote'))
    proc = _real_worker_with_descendant(tmp_path, lock, markers)
    try:
        assert provider_activity.is_active(lock) is True
        proc.kill()
        proc.wait(timeout=10)
        # The lock itself is now free -- the kernel dropped it with the wrapper.
        assert provider_activity.registered(lock) == proc.pid
        assert provider_activity.group_alive(proc.pid) is True
        assert provider_activity.is_active(lock) is True, (
            'a released lock was read as an exit while the descendant still writes')
        markers[1].touch()  # let the descendant do its write
        deadline = time.monotonic() + 5
        while not markers[2].exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert markers[2].exists(), 'premise: the descendant really does write'
        assert provider_activity.is_active(lock) is True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while provider_activity.group_alive(proc.pid) is not False and time.monotonic() < deadline:
        time.sleep(.05)
    # Whole tree gone: the site is free again, and the stale record does not
    # outlive it -- `is_active` answers from the kernel, not from the file.
    assert provider_activity.is_active(lock) is False


def test_an_old_generation_cannot_finish_the_round_that_replaced_it(tmp_path):
    """`finish` retires only the generation its caller claimed.

    The sequence a restart actually produces: a worker claims generation 1, the run
    is re-enqueued as generation 2 and claimed by a new worker, and only then does
    the old worker's `finally` run. Before the generation predicate that `finish`
    marked the job done, so generation 2 -- already running -- was retired by a
    process that never touched it, and dispatch had nothing left to hand back.
    """
    queue = DurableQueue(Store(tmp_path / 'control.db'))
    assert queue.enqueue('r1', 'execute') is True
    first = queue.claim('r1', 'execute')
    assert first == 1
    # A new round for the same run: enqueue bumps the generation.
    assert queue.enqueue('r1', 'execute', continuation=True) is True
    second = queue.claim('r1', 'execute')
    assert second == 2

    # The old worker finally reaches its finally block.
    assert queue.finish('r1', 'execute', first) is False
    with queue.store.connect() as db:
        row = db.execute('SELECT status,generation FROM control_jobs WHERE run_id=?',
                         ('r1',)).fetchone()
    assert (row['status'], row['generation']) == ('running', 2)

    # The worker that did claim it can retire it, once.
    assert queue.finish('r1', 'execute', second) is True
    assert queue.finish('r1', 'execute', second) is False


def test_a_delayed_claim_against_a_replaced_generation_takes_nothing(tmp_path):
    """A claim reports which generation it took, so a later finish is checkable.

    `claim` returned a bool, which let a caller hold a running job with no way to
    name it. The returned generation is what makes the later `finish` checkable.
    """
    queue = DurableQueue(Store(tmp_path / 'control.db'))
    queue.enqueue('r1', 'execute')
    assert queue.claim('r1', 'execute') == 1
    # Nothing pending now: a second dispatcher pass claims nothing at all.
    assert queue.claim('r1', 'execute') is None
    queue.enqueue('r1', 'execute', continuation=True)
    assert queue.claim('r1', 'execute') == 2
