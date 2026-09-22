"""Tests for the maintenance-subsystem subcommands added to ``maintenance_cli``.

Strategy: a real temp git repo under a temp workspace root, a temp data dir,
and ``maintenance_cli.main([...])`` invoked in-process with stdout captured
via ``capsys`` -- so each command exercises the real ``MaintenanceSubsystem``
core and the real (enqueue-only) ``Service``, the same way the production CLI
does, without paying for a model call.

No test here calls a real model. The optional runtime test constructs the
standalone runtime through ``maintenance_cli.run_runtime`` with an injected
fake runner (``tests.test_control_app.FakeSDK``), never the module-level
``runtime`` subcommand that reads live provider configuration.
"""
from __future__ import annotations

import getpass
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from factory.control import maintenance_cli
from factory.control.store import Store
from tests.test_control_app import FakeSDK

OS_USER = getpass.getuser()


def _git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True,
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _reset_service_cache():
    """Each CLI invocation ordinarily pops and closes its own cached Service
    (see ``maintenance_cli.main``'s ``finally``), but calling functions
    directly (as ``run_runtime`` does) bypasses that. Belt and suspenders so
    one test's leftover cache can never leak into the next."""
    maintenance_cli._service_cache.pop('svc', None)
    yield
    svc = maintenance_cli._service_cache.pop('svc', None)
    if svc is not None:
        svc.close()


@pytest.fixture
def env(tmp_path):
    workspace_root = tmp_path / 'projects'
    workspace_root.mkdir()
    data_dir = tmp_path / 'data'

    repo = workspace_root / 'sample'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.email', 'f@l')
    _git(repo, 'config', 'user.name', 'F')
    (repo / 'README.md').write_text('# sample\n')
    (repo / 'pyproject.toml').write_text('[project]\nname="sample"\n')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_x.py').write_text('def test_x():\n    assert True\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'baseline')

    class _NS:
        pass
    ns = _NS()
    ns.workspace_root, ns.data_dir, ns.repo = workspace_root, data_dir, repo
    ns.db = data_dir / 'control.db'
    return ns


def _run(env, capsys, *args, expect_rc=0):
    full = ['--data-dir', str(env.data_dir), '--workspace-root', str(env.workspace_root),
            '--operator', OS_USER, *args]
    rc = maintenance_cli.main(full)
    out = capsys.readouterr()
    stream = out.out if rc == 0 else out.err
    data = json.loads(stream) if stream.strip() else {}
    assert rc == expect_rc, f'args={args} rc={rc} stdout={out.out!r} stderr={out.err!r}'
    return data


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
def test_init_creates_data_dir_and_db(env, capsys):
    data = _run(env, capsys, 'init')
    assert Path(data['data_dir']) == env.data_dir
    assert Path(data['db']).is_file()


def test_query_command_refuses_missing_db(env, capsys):
    data = _run(env, capsys, 'repo', 'list', expect_rc=1)
    assert '数据库不存在' in data['error']


# ---------------------------------------------------------------------------
# repo
# ---------------------------------------------------------------------------
def test_repo_add_local_path_reaches_needs_input(env, capsys):
    _run(env, capsys, 'init')
    data = _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    assert data['state'] == 'needs_input'
    assert data['repository']
    assert 'project_id' in data
    assert any('检查命令' in n for n in data['needs'])


def test_repo_adopt_checks_moves_to_ready(env, capsys):
    _run(env, capsys, 'init')
    added = _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    pid = added['project_id']
    suggested = [c['name'] for c in (added['probe'] or {}).get('suggested_checks', [])]
    assert suggested, 'the seeded repo has a tests/ dir + pyproject.toml, so pytest should be suggested'
    data = _run(env, capsys, 'repo', 'adopt-checks', pid, *suggested)
    assert data['state'] == 'ready'
    assert data['checks_configured']


def test_repo_list_and_show(env, capsys):
    _run(env, capsys, 'init')
    added = _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    pid = added['project_id']
    listed = _run(env, capsys, 'repo', 'list')
    assert any(r['project_id'] == pid for r in listed['repos'])
    shown = _run(env, capsys, 'repo', 'show', pid)
    assert shown['project_id'] == pid
    assert 'requirements' in shown and 'tasks' in shown


def test_repo_probe_rereports_state(env, capsys):
    _run(env, capsys, 'init')
    added = _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    pid = added['project_id']
    probed = _run(env, capsys, 'repo', 'probe', pid)
    assert probed['project_id'] == pid
    assert probed['probe']['access']['ok'] is True


# ---------------------------------------------------------------------------
# submit / requirements / dispatch
# ---------------------------------------------------------------------------
def _ready_project(env, capsys):
    added = _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    pid = added['project_id']
    suggested = [c['name'] for c in (added['probe'] or {}).get('suggested_checks', [])]
    _run(env, capsys, 'repo', 'adopt-checks', pid, *suggested)
    return pid


def test_submit_returns_dispatched_receipt_with_offline_executor(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    data = _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
               '--idempotency-key', 'submit-test-key-1')
    assert data['status'] == 'dispatched'
    assert data['task_id']
    assert data['duplicate'] is False
    assert data['executor']['status'] == 'offline'


def test_submit_duplicate_idempotency_key_is_flagged(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    first = _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
                '--idempotency-key', 'dup-key-1')
    second = _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
                 '--idempotency-key', 'dup-key-1')
    assert second['duplicate'] is True
    assert second['requirement_id'] == first['requirement_id']
    assert second['task_id'] == first['task_id']


def test_submit_from_file(env, capsys, tmp_path):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    req_file = tmp_path / 'req.txt'
    req_file.write_text('导出报表偶发缺行')
    data = _run(env, capsys, 'submit', '--project', pid, '--file', str(req_file),
               '--idempotency-key', 'file-submit-1')
    assert data['status'] == 'dispatched'


def test_requirements_lists_submitted_items(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
        '--idempotency-key', 'req-list-1')
    data = _run(env, capsys, 'requirements', '--project', pid)
    assert isinstance(data['requirements'], list)
    assert len(data['requirements']) == 1
    assert data['requirements'][0]['project_id'] == pid


def test_submit_no_dispatch_then_manual_dispatch(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    received = _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
                    '--no-dispatch', '--idempotency-key', 'manual-dispatch-1')
    assert received['status'] == 'pending_dispatch'
    assert received['task_id'] is None
    dispatched = _run(env, capsys, 'dispatch', received['requirement_id'])
    assert dispatched['status'] == 'dispatched'
    assert dispatched['task_id']


# ---------------------------------------------------------------------------
# overview / graph
# ---------------------------------------------------------------------------
def test_overview_counts_running_task_not_yet_executed(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
        '--idempotency-key', 'overview-1')
    data = _run(env, capsys, 'overview', '--project', pid)
    assert data['counts']['running'] == 1
    assert data['contract_version']


def test_graph_projection_has_repo_and_task_nodes(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
        '--idempotency-key', 'graph-test-1')
    data = _run(env, capsys, 'graph', '--project', pid)
    types = {n['type'] for n in data['nodes']}
    assert 'repo' in types
    assert 'task' in types


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------
def test_actions_includes_cancel_not_pause(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    submitted = _run(env, capsys, 'submit', '--project', pid, '--text', '登录页偶发白屏',
                     '--idempotency-key', 'actions-1')
    data = _run(env, capsys, 'actions', submitted['task_id'])
    assert 'cancel' in data['actions']
    assert 'pause' not in data['actions']


# ---------------------------------------------------------------------------
# version / manifest
# ---------------------------------------------------------------------------
def test_version(env, capsys):
    _run(env, capsys, 'init')
    data = _run(env, capsys, 'version')
    from factory.control.maintenance_subsystem import VERSION, CONTRACT_VERSION
    assert data == {'version': VERSION, 'contract_version': CONTRACT_VERSION}


def test_manifest(env, capsys):
    _run(env, capsys, 'init')
    data = _run(env, capsys, 'manifest')
    assert data['plugin_id'] == 'issue-maintenance'
    assert data['supports_pause'] is False
    assert data['entries']['cli'] == 'webuddy-maintenance'


# ---------------------------------------------------------------------------
# optional: the standalone runtime, with an injected fake runner
# ---------------------------------------------------------------------------
def _wait_for(fn, deadline_s=15, interval=0.1):
    deadline = time.time() + deadline_s
    result = fn()
    while time.time() < deadline and not result[1]:
        time.sleep(interval)
        result = fn()
    return result[0]


def test_runtime_executes_the_queue_and_export_writes_a_patch(env, capsys, tmp_path, monkeypatch):
    """submit (enqueues) → runtime executes the plan → approve → runtime
    finishes it → export writes a real .patch.

    ``run_runtime`` is called directly with an injected fake runner
    (``FakeSDK``) so no real model call happens; production ``runtime``
    (invoked via ``main``) always uses the configured runtime instead.

    ``FakeSDK`` (``tests/test_control_app.py``) always proposes a plan with
    one task named ``greeting`` that writes ``greeting.txt`` and requires a
    check also named ``greeting`` -- so that exact check is wired onto the
    project directly through the store, the same way
    ``tests/test_control_app.py::project`` does, rather than through
    ``repo adopt-checks`` (which only offers checks the repo probe actually
    detected, and this repo's stack does not include one named ``greeting``).
    """
    # A real install configures its model profiles (INSTALL.md); the first data
    # domain write seeds them from the environment.
    for role in ('PLANNER', 'CHEAP', 'STANDARD', 'STRONG'):
        monkeypatch.setenv(f'FACTORY_{role}_PROVIDER', 'codex')
        monkeypatch.setenv(f'FACTORY_{role}_MODEL', 'test')
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    store = Store(env.db)
    project = store.project(pid)
    store.update_project(pid, {'checks': {**project['checks'], 'greeting': [
        sys.executable, '-c',
        "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello world'"]}},
        project['revision'], 'test')

    submitted = _run(env, capsys, 'submit', '--project', pid, '--text', '更新问候语',
                     '--idempotency-key', 'runtime-1')
    task_id = submitted['task_id']

    stop_event = threading.Event()
    runtime_result = {}

    def _serve():
        runtime_result['rc'] = maintenance_cli.run_runtime(
            env.db, workspace_root=str(env.workspace_root), runner=FakeSDK(),
            stop_event=stop_event, print_fn=lambda *a, **kw: None)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        def _current():
            view = _run(env, capsys, 'actions', task_id)
            return view, view['status'] in ('waiting', 'delivered', 'failed')
        view = _wait_for(_current)
        assert view['status'] in ('waiting', 'delivered'), view

        if view['status'] == 'waiting':
            assert 'approve' in view['actions'], view
            _run(env, capsys, 'approve', task_id)

            def _delivered():
                view = _run(env, capsys, 'actions', task_id)
                return view, view['status'] in ('delivered', 'failed')
            view = _wait_for(_delivered)
        assert view['status'] == 'delivered', view
    finally:
        stop_event.set()
        thread.join(timeout=10)

    out_dir = tmp_path / 'export'
    exported = _run(env, capsys, 'export', task_id, '--output', str(out_dir))
    written = exported['written']
    assert len(written) >= 1
    assert Path(written[0]).is_file()
    assert Path(written[0]).stat().st_size > 0


def test_events_follow_streams_ndjson_until_terminal(env, capsys):
    """--follow must reach the streaming handler (a legacy branch once shadowed it)."""
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    receipt = _run(env, capsys, 'submit', '--project', pid, '--text', '跟读事件',
                   '--idempotency-key', 'follow-key-0001')
    _run(env, capsys, 'cancel', receipt['task_id'])
    rc = maintenance_cli.main(['--data-dir', str(env.data_dir), '--workspace-root', str(env.workspace_root),
                               '--operator', OS_USER, 'events', receipt['task_id'], '--follow',
                               '--interval', '0.01'])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert rc == 0
    assert lines[-1]['followed'] is True
    streamed = lines[:-1]
    assert streamed and all('sequence' in e and 'kind' in e for e in streamed)
    assert streamed[0]['kind'] == 'user.message'


def test_version_and_manifest_need_no_data_domain(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('FACTORY_CONTROL_DATA', str(tmp_path / 'absent'))
    assert maintenance_cli.main(['version']) == 0
    assert json.loads(capsys.readouterr().out)['contract_version'] == 'maintenance-subsystem/1'
    assert maintenance_cli.main(['manifest']) == 0
    assert json.loads(capsys.readouterr().out)['supports_pause'] is False
    assert not (tmp_path / 'absent').exists()


def test_first_cli_command_seeds_runtime_settings_from_environment(env, capsys, monkeypatch):
    """The CLI must not persist placeholder model profiles the runtime will execute with."""
    for role in ('PLANNER', 'CHEAP', 'STANDARD', 'STRONG'):
        monkeypatch.setenv(f'FACTORY_{role}_PROVIDER', 'claude')
        monkeypatch.setenv(f'FACTORY_{role}_MODEL', 'claude-sonnet-5')
    _run(env, capsys, 'init')
    _run(env, capsys, 'repo', 'add', '--source', str(env.repo), '--name', 'Sample')
    from factory.control.runtime import RuntimeSettings
    from factory.control.store import Store
    profiles = RuntimeSettings(Store(env.db)).get()['profiles']
    assert {p['provider'] for p in profiles.values()} == {'claude'}
    assert {p['model'] for p in profiles.values()} == {'claude-sonnet-5'}


def test_show_returns_the_same_enriched_view_as_the_page(env, capsys):
    _run(env, capsys, 'init')
    pid = _ready_project(env, capsys)
    receipt = _run(env, capsys, 'submit', '--project', pid, '--text', '同一视图',
                   '--idempotency-key', 'show-view-0001')
    shown = _run(env, capsys, 'show', receipt['task_id'])
    for key in ('actions', 'pending_plan', 'pending_questions', 'supplements', 'resumable'):
        assert key in shown
    assert 'cancel' in shown['actions'] and 'pause' not in shown['actions']
