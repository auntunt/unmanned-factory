"""Project command tool for Claude SDK; every command runs inside bubblewrap.

The SDK process retains provider authentication. Its command child receives only
an explicit environment and cannot see the host home, temporary files or sockets.
Git integration remains coordinator-owned, so shared Git metadata is read-only.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

TOOL_NAME = 'mcp__project__run_command'


def available() -> bool:
    if not sys.platform.startswith('linux'):
        return False
    from factory.harness.sandbox_linux import available as probe
    return probe()


def command_argv(workspace: Path, scratch: Path, command: str) -> list[str]:
    from factory.harness.sandbox_linux import SANDBOX_BINARY, git_dir
    workspace, scratch = workspace.resolve(), scratch.resolve()
    if not workspace.is_dir() or workspace == Path('/'):
        raise ValueError('A project workspace directory is required')
    argv = [SANDBOX_BINARY, '--die-with-parent', '--new-session',
            '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
            '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
    for path in ('/home', '/root', '/tmp', '/var/tmp', '/run'):
        if Path(path).exists():
            argv += ['--tmpfs', path]
    argv += ['--bind', str(scratch), '/tmp', '--bind', str(workspace), str(workspace)]
    if Path('/etc/resolv.conf').exists():
        resolved_dns = str(Path('/etc/resolv.conf').resolve())
        argv += ['--ro-bind', resolved_dns, resolved_dns]
    common = git_dir(workspace)
    if common:
        argv += ['--ro-bind', str(common), str(common)]
    marker = workspace / '.git'
    if marker.exists():
        argv += ['--ro-bind', str(marker), str(marker)]
    # uv is a standalone dependency installer, often installed under host HOME.
    uv = shutil.which('uv')
    if not uv and (Path.home() / '.local/bin/uv').is_file():
        uv = str(Path.home() / '.local/bin/uv')
    if uv:
        argv += ['--ro-bind', str(Path(uv).resolve()), '/tmp/.webuddy-bin/uv']
    argv += ['--clearenv', '--setenv', 'PATH', '/tmp/.webuddy-bin:/usr/local/bin:/usr/bin:/bin',
             '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp',
             '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'UV_CACHE_DIR', '/tmp/uv-cache',
             '--setenv', 'PIP_DISABLE_PIP_VERSION_CHECK', '1',
             '--chdir', str(workspace), '--', '/bin/bash', '--noprofile', '--norc', '-c', command]
    return argv


def run_command(workspace, command, timeout=300, emit=None):
    from factory.control.resources import command_slot
    import time
    emit = emit or (lambda *args: None)
    started = time.monotonic()
    emit('task.activity', {'phase': 'waiting_capacity'})
    with command_slot(max(1, min(int(timeout), 3600))) as waited:
        emit('task.activity', {'phase': 'command', 'wait_s': round(waited, 3)})
        try:
            result = _run_command_unlimited(workspace, command, max(1, timeout - waited))
            return {**result, 'wait_s': round(waited, 3), 'duration_s': round(time.monotonic() - started, 3)}
        finally:
            emit('task.activity', {'phase': 'model'})


def _run_command_unlimited(workspace: Path, command: str, timeout: int = 300) -> dict:
    from factory.control.store import scrub
    from factory.permission.rules import check_command, Decision
    if not isinstance(command, str) or not command.strip() or len(command) > 20000:
        raise ValueError('command must be non-empty and at most 20000 characters')
    ruling = check_command(command)
    if ruling.decision != Decision.ALLOW:
        return {'exit_code': None, 'error': ruling.reason, 'rule': ruling.rule}
    if not available():
        raise RuntimeError('Project terminal requires working bubblewrap; unisolated execution is disabled')
    with tempfile.TemporaryDirectory(prefix='webuddy-command-') as directory:
        argv = command_argv(workspace, Path(directory), command)
        # Spool command output to disk rather than retaining unbounded output in RAM.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                    env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, close_fds=True)
            timed_out = False
            try:
                proc.wait(timeout=max(1, min(int(timeout), 3600)))
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
            output.seek(0, os.SEEK_END)
            size = output.tell()
            output.seek(max(0, size - 30000))
            text = output.read().decode('utf-8', errors='replace')
        return {'exit_code': proc.returncode, 'output': scrub(text),
                'timeout': timed_out, 'truncated': size > 30000}


def create_server(workspace: Path, emit=None):
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool('run_command', 'Run a shell command inside the project sandbox. Use for dependency installation, tests, builds and local checks. Host home and credentials are hidden. Git commits are handled by webuddy. Default timeout 300 seconds; maximum 3600 seconds.',
          {'type': 'object', 'properties': {'command': {'type': 'string'}, 'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': 3600}}, 'required': ['command'], 'additionalProperties': False})
    async def terminal(args):
        try:
            result = await asyncio.to_thread(run_command, workspace, args['command'], args.get('timeout_s', 300), emit)
        except (ValueError, RuntimeError, OSError) as exc:
            result = {'exit_code': None, 'error': str(exc)}
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                'is_error': result.get('exit_code') != 0}

    return create_sdk_mcp_server('project', tools=[terminal])
