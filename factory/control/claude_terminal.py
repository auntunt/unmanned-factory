"""Project command tool for Claude SDK; every command runs inside bubblewrap.

The SDK process retains provider authentication. Its command child receives only
an explicit environment and cannot see the host home, temporary files or sockets.
Git integration remains coordinator-owned, so shared Git metadata is read-only.
"""
from __future__ import annotations

import asyncio
import hashlib
import stat
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import selectors

TOOL_NAME = 'mcp__project__run_command'


def available() -> bool:
    if not sys.platform.startswith('linux'):
        return False
    from factory.harness.sandbox_linux import available as probe
    return probe()


def dependency_cache(workspace: Path) -> tuple[Path, Path]:
    """Private per-repository caches, shared only by that repository's worktrees."""
    from factory.harness.sandbox_linux import git_dir
    workspace = workspace.resolve()
    root = Path(os.getenv('WEBUDDY_DEPENDENCY_CACHE_ROOT',
                          str(Path.home() / '.cache/webuddy/dependencies'))).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('Dependency cache root must be an absolute dedicated directory, not a symlink')
    root = root.resolve()
    if root == Path.home().resolve() or root == Path('/') or workspace == root or workspace.is_relative_to(root) or root.is_relative_to(workspace):
        raise ValueError('Dependency cache must be separate from the project workspace and host home')
    common = git_dir(workspace)
    identity = 'git:' + str(common.resolve()) if common else 'workspace:' + str(workspace)
    project = root / hashlib.sha256(identity.encode()).hexdigest()
    for path in (root, project, *(project / name for name in ('npm', 'pip', 'uv'))):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError('Dependency cache must be a private directory owned by the service user')
        path.chmod(0o700)
    return root, project


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
    cache_root, project_cache = dependency_cache(workspace)
    # Hide the whole host cache tree, exposing only this repository's cache.
    argv += ['--tmpfs', str(cache_root), '--bind', str(scratch), '/tmp',
             '--bind', str(workspace), str(workspace),
             '--bind', str(project_cache), '/tmp/.webuddy-cache']
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
             '--setenv', 'LANG', 'C.UTF-8',
             '--setenv', 'WEBUDDY_DEPENDENCY_CACHE', '/tmp/.webuddy-cache',
             '--setenv', 'npm_config_cache', '/tmp/.webuddy-cache/npm',
             '--setenv', 'PIP_CACHE_DIR', '/tmp/.webuddy-cache/pip',
             '--setenv', 'UV_CACHE_DIR', '/tmp/.webuddy-cache/uv',
             '--setenv', 'PIP_DISABLE_PIP_VERSION_CHECK', '1',
             '--chdir', str(workspace), '--', '/bin/bash', '--noprofile', '--norc', '-c', command]
    return argv



# The supervisor remains PID 1's child for the whole SDK execution. Each shell
# gets its own process group, but normal completion leaves preview children alive.
_SUPERVISOR = r"""
import json, os, signal, subprocess, sys, tempfile
for line in sys.stdin:
    request = json.loads(line)
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', request['command']],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            child.wait(timeout=request['timeout'])
        except subprocess.TimeoutExpired:
            timed_out = True
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 30000))
        result = dict(exit_code=child.returncode, output=output.read().decode('utf-8', errors='replace'),
            timeout=timed_out, truncated=size > 30000)
    print(json.dumps(result), flush=True)
"""


class TerminalSession:
    """One isolated process/tmp lifetime per SDK execution, never host execution."""
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.lock = threading.RLock()
        self.proc = None
        self.scratch = None
        self.closed = False

    def _start(self):
        if self.closed:
            raise RuntimeError('Project terminal session is closed')
        if self.proc is not None:
            if self.proc.poll() is not None:
                raise RuntimeError('Project terminal session ended; restart execution to restore services')
            return
        if not available():
            raise RuntimeError('Project terminal requires working bubblewrap; unisolated execution is disabled')
        self.scratch = tempfile.TemporaryDirectory(prefix='webuddy-session-')
        argv = command_argv(self.workspace, Path(self.scratch.name), '')
        argv = argv[:-5] + ['/usr/bin/python3', '-u', '-c', _SUPERVISOR]
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, close_fds=True)
        except Exception:
            self.scratch.cleanup()
            self.scratch = None
            raise

    def run(self, command, timeout):
        from factory.permission.rules import check_command, Decision
        from factory.control.store import scrub
        if not isinstance(command, str) or not command.strip() or len(command) > 20000:
            raise ValueError('command must be non-empty and at most 20000 characters')
        ruling = check_command(command)
        if ruling.decision != Decision.ALLOW:
            return {'exit_code': None, 'error': ruling.reason, 'rule': ruling.rule}
        with self.lock:
            self._start()
            timeout = max(1, min(float(timeout), 3600))
            try:
                self.proc.stdin.write((json.dumps({'command': command, 'timeout': timeout}) + '\n').encode())
                self.proc.stdin.flush()
                with selectors.DefaultSelector() as ready:
                    ready.register(self.proc.stdout, selectors.EVENT_READ)
                    if not ready.select(timeout + 10):
                        self.close()
                        raise RuntimeError('Project terminal supervisor did not respond; session closed')
                line = self.proc.stdout.readline(200000)
                if not line or not line.endswith(b'\n'):
                    raise RuntimeError('Project terminal session ended without a command result')
                result = json.loads(line)
                result['output'] = scrub(result.get('output', ''))
                return result
            except Exception:
                self.close()
                raise

    def close(self):
        with self.lock:
            self.closed = True
            if self.proc is not None:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try: self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait()
                self.proc.stdin.close()
                self.proc.stdout.close()
            if self.scratch is not None:
                self.scratch.cleanup()


def run_command(workspace, command, timeout=300, emit=None, session=None):
    from factory.control.resources import command_slot
    import time
    emit = emit or (lambda *args: None)
    started = time.monotonic()
    emit('task.activity', {'phase': 'waiting_capacity'})
    with command_slot(max(1, min(int(timeout), 3600))) as waited:
        emit('task.activity', {'phase': 'command', 'wait_s': round(waited, 3)})
        try:
            result = (session.run(command, max(1, timeout - waited)) if session is not None
                      else _run_command_unlimited(workspace, command, max(1, timeout - waited)))
            result = {**result, 'wait_s': round(waited, 3), 'duration_s': round(time.monotonic() - started, 3)}
            from factory.control.store import scrub
            emit('command.completed', {
                'command': scrub(command)[:2000], 'exit_code': result.get('exit_code'),
                'timeout': bool(result.get('timeout')), 'duration_s': result['duration_s'],
                'wait_s': result['wait_s'],
                'execution_s': round(max(0, result['duration_s'] - waited), 3),
                'output': scrub(str(result.get('output', '')))[-4000:],
                'error': scrub(str(result.get('error', '')))[:1000],
                'truncated': bool(result.get('truncated')) or len(str(result.get('output', ''))) > 4000,
                'source': 'isolated_project_terminal',
            })
            return result
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


def create_server(workspace: Path, emit=None, session=None, browser_session=None):
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool('run_command', 'Run a shell command inside the project sandbox. Use for dependency installation, tests, builds and local checks. Within this execution, background services and /tmp persist across calls. Shell cwd and exports do not persist: use explicit paths. Temporary files reset when execution ends or restarts; save durable evidence in the project. Dependency caches persist across retries for this project only: npm_config_cache, PIP_CACHE_DIR and UV_CACHE_DIR point into /tmp/.webuddy-cache. Host home and credentials are hidden. Git commits are handled by webuddy. Default timeout 300 seconds; maximum 3600 seconds.',
          {'type': 'object', 'properties': {'command': {'type': 'string'}, 'timeout_s': {'type': 'integer', 'minimum': 1, 'maximum': 3600}}, 'required': ['command'], 'additionalProperties': False})
    async def terminal(args):
        try:
            result = await asyncio.to_thread(run_command, workspace, args['command'], args.get('timeout_s', 300), emit, session)
        except (ValueError, RuntimeError, OSError) as exc:
            result = {'exit_code': None, 'error': str(exc)}
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                'is_error': result.get('exit_code') != 0}

    from factory.control.project_browser import create_tools
    browser_tools = create_tools(browser_session, emit) if browser_session is not None else []
    return create_sdk_mcp_server('project', tools=[terminal, *browser_tools])
