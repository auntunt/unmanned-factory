from pathlib import Path
import pytest
from factory.control import claude_terminal


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
