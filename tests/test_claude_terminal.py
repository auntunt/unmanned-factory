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
