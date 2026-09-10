from pathlib import Path
import pytest
from factory.control import claude_terminal


@pytest.fixture(autouse=True)
def private_cache_root(monkeypatch, tmp_path):
    monkeypatch.setenv('WEBUDDY_DEPENDENCY_CACHE_ROOT', str(tmp_path / 'host-private-cache'))


def test_terminal_refuses_unisolated_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_terminal, 'available', lambda: False)
    with pytest.raises(RuntimeError, match='unisolated execution is disabled'):
        claude_terminal.run_command(tmp_path, 'python3 -m unittest')


def test_terminal_rejects_destructive_history_reset(tmp_path):
    result = claude_terminal.run_command(tmp_path, 'git reset --hard')
    assert result['rule'] == 'git-reset-hard'


def test_terminal_masks_host_and_preserves_tmp_workspace_mount_order(tmp_path):
    workspace = tmp_path / 'workspace'
    scratch = tmp_path / 'scratch'
    workspace.mkdir();scratch.mkdir()
    argv = claude_terminal.command_argv(workspace, scratch, 'python3 -m unittest')
    assert argv.index(str(scratch)) < argv.index(str(workspace))
    assert '--clearenv' in argv and '--unshare-pid' in argv and '--die-with-parent' in argv
    pairs = list(zip(argv, argv[1:]))
    assert all(('--tmpfs', p) in pairs for p in ['/home', '/root', '/run', '/tmp'] if Path(p).exists())
    assert ('--ro-bind', '/') in pairs
    assert argv[-1] == 'python3 -m unittest'


def test_persistent_session_refuses_unisolated_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_terminal, 'available', lambda: False)
    session = claude_terminal.TerminalSession(tmp_path)
    with pytest.raises(RuntimeError, match='unisolated execution is disabled'):
        session.run('echo ok', 5)
    session.close()


def test_persistent_session_checks_policy_before_start(tmp_path):
    session = claude_terminal.TerminalSession(tmp_path)
    assert session.run('git reset --hard', 5)['rule'] == 'git-reset-hard'
    assert session.proc is None
    session.close()


@pytest.mark.skipif(not claude_terminal.available(), reason='requires Linux bubblewrap')
def test_session_preserves_preview_and_tmp_then_cleans_up(tmp_path):
    import shlex
    import urllib.request
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'index.html').write_text('persistent-preview-ok')
    server = "import http.server,pathlib; s=http.server.HTTPServer(('127.0.0.1',0),http.server.SimpleHTTPRequestHandler); pathlib.Path('/tmp/port').write_text(str(s.server_port)); s.serve_forever()"
    session = claude_terminal.TerminalSession(workspace)
    other = claude_terminal.TerminalSession(workspace)
    try:
        start = session.run('python3 -u -c ' + shlex.quote(server) + ' >/tmp/preview.log 2>&1 &', 10)
        assert start['exit_code'] == 0
        result = session.run('for i in {1..50}; do test -f /tmp/port && break; sleep .1; done; cat /tmp/port', 10)
        port = int(result['output'].strip())
        assert urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=5).read() == b'persistent-preview-ok'
        assert other.run('test ! -e /tmp/port', 5)['exit_code'] == 0
        assert session.run('sleep 10', 1)['timeout'] is True
        assert session.run('cat /tmp/port', 5)['output'].strip() == str(port)
        assert urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=5).status == 200
        scratch = Path(session.scratch.name)
        session.close()
        assert not scratch.exists()
        with pytest.raises(Exception):
            urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=2)
        with pytest.raises(RuntimeError, match='closed'):
            session.run('echo should-not-run', 5)
    finally:
        session.close()
        other.close()


def test_cache_reuses_repository_across_worktrees_without_project_sharing(tmp_path):
    import subprocess
    import stat
    def git(root, *args):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'base')
    worktree = tmp_path / 'retry'
    git(repo, 'worktree', 'add', '-b', 'retry', str(worktree))
    root, first = claude_terminal.dependency_cache(repo)
    _, retry = claude_terminal.dependency_cache(worktree)
    assert first == retry
    (first / 'npm' / 'cached-package').write_text('persistent bytes')
    assert (claude_terminal.dependency_cache(worktree)[1] / 'npm' / 'cached-package').read_text() == 'persistent bytes'
    other = tmp_path / 'other'
    other.mkdir()
    git(other, 'init', '-q', '-b', 'main')
    _, separate = claude_terminal.dependency_cache(other)
    assert separate != first and not (separate / 'npm' / 'cached-package').exists()
    for directory in (root, first, first / 'npm', first / 'pip', first / 'uv'):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_cache_mount_and_env_expose_only_current_project(tmp_path):
    workspace, scratch = tmp_path / 'project', tmp_path / 'scratch'
    workspace.mkdir(); scratch.mkdir()
    root, cache = claude_terminal.dependency_cache(workspace)
    argv = claude_terminal.command_argv(workspace, scratch, 'env')
    triples = list(zip(argv, argv[1:], argv[2:]))
    pairs = list(zip(argv, argv[1:]))
    assert ('--tmpfs', str(root)) in pairs
    assert ('--bind', str(cache), '/tmp/.webuddy-cache') in triples
    assert ('--setenv', 'npm_config_cache', '/tmp/.webuddy-cache/npm') in triples
    assert ('--setenv', 'PIP_CACHE_DIR', '/tmp/.webuddy-cache/pip') in triples
    assert ('--setenv', 'UV_CACHE_DIR', '/tmp/.webuddy-cache/uv') in triples
    assert ('--setenv', 'HOME', '/tmp') in triples
    assert '--clearenv' in argv


def test_cache_rejects_symlink_and_workspace_cache_root(monkeypatch, tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    monkeypatch.setenv('WEBUDDY_DEPENDENCY_CACHE_ROOT', str(workspace / '.cache'))
    with pytest.raises(ValueError, match='separate'):
        claude_terminal.dependency_cache(workspace)
    root = tmp_path / 'cache'
    monkeypatch.setenv('WEBUDDY_DEPENDENCY_CACHE_ROOT', str(root))
    _, project_cache = claude_terminal.dependency_cache(workspace)
    (project_cache / 'npm').rmdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (project_cache / 'npm').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError):
        claude_terminal.dependency_cache(workspace)


@pytest.mark.skipif(not claude_terminal.available(), reason='requires Linux bubblewrap')
def test_real_sessions_reuse_cache_without_host_credentials_or_cross_project_writes(tmp_path, monkeypatch):
    first, other = tmp_path / 'first', tmp_path / 'other'
    first.mkdir(); other.mkdir()
    monkeypatch.setenv('NPM_TOKEN', 'host-credential-must-not-leak')
    session = claude_terminal.TerminalSession(first)
    try:
        assert session.run('printf cached > "$npm_config_cache/package"; printf pip > "$PIP_CACHE_DIR/package"; printf uv > "$UV_CACHE_DIR/package"', 10)['exit_code'] == 0
    finally:
        session.close()
    retry, separate = claude_terminal.TerminalSession(first), claude_terminal.TerminalSession(other)
    try:
        read = retry.run('cat "$npm_config_cache/package" "$PIP_CACHE_DIR/package" "$UV_CACHE_DIR/package"; test -z "$NPM_TOKEN"', 10)
        assert read['exit_code'] == 0 and 'cachedpipuv' in read['output']
        assert separate.run('test ! -e "$npm_config_cache/package"', 10)['exit_code'] == 0
        cache_root, first_cache = claude_terminal.dependency_cache(first)
        import shlex
        assert separate.run('test ! -e ' + shlex.quote(str(first_cache / 'npm' / 'package')), 10)['exit_code'] == 0
    finally:
        retry.close(); separate.close()


@pytest.mark.skipif(not claude_terminal.available(), reason='requires Linux bubblewrap')
def test_real_npm_offline_cache_survives_terminal_restart(tmp_path):
    import io
    import shutil
    import tarfile
    if not shutil.which('npm'):
        pytest.skip('npm is not installed on this host')
    workspace = tmp_path / 'npm-project'
    workspace.mkdir()
    package = b'{"name":"webuddy-cache-fixture","version":"1.0.0"}'
    with tarfile.open(workspace / 'fixture.tgz', 'w:gz') as archive:
        entry = tarfile.TarInfo('package/package.json')
        entry.size = len(package)
        archive.addfile(entry, io.BytesIO(package))
    first = claude_terminal.TerminalSession(workspace)
    try:
        result = first.run('npm cache add ./fixture.tgz --ignore-scripts --offline', 20)
        assert result['exit_code'] == 0, result
    finally:
        first.close()
    retry = claude_terminal.TerminalSession(workspace)
    try:
        result = retry.run('npm cache ls --offline', 20)
        assert result['exit_code'] == 0 and 'fixture.tgz' in result['output'], result
        root, cache = claude_terminal.dependency_cache(workspace)
        assert (cache / 'npm' / '_cacache').is_dir()
    finally:
        retry.close()
