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
