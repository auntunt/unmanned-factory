"""Bounded, evidence producing execution of a planned task DAG.

This module deliberately keeps the provider boundary small.  Providers edit an
isolated worktree; all Git operations, checks, scope validation, and integration
are performed here with argv based subprocesses.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import math
import os
import re
import signal
import subprocess
import tempfile
import shutil
import tomllib
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any

from factory.harness.checkenv import check_env
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.harness.workspace import (
    changed_config,
    changed_hooks,
    diff_hash,
    diff_suppressed,
    git_config,
    head_position,
    index_skipped,
    info_attributes,
    newly_skipped,
    replace_refs,
    shadow_code,
    staged_gitlinks,
    runner_hooks,
)
from factory.harness.verdict_probe import judge_files_touched


_MAX_OUTPUT = 4_000
_SAFE_REF = re.compile(r"^[A-Za-z0-9_-]+$")
_GENERATED_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules"}
)
_GENERATED_NAMES = frozenset({".coverage", "coverage.xml"})
_FORBIDDEN_PARTS = frozenset({".git", ".github"})
_FORBIDDEN_NAMES = frozenset(
    {".env", ".env.local", ".env.production", ".env.development", "id_rsa", "id_ed25519"}
)


class ExecutionError(RuntimeError):
    """An execution attempt could not produce a verified delivery commit."""

    def __init__(self, message: str, *, artifacts: dict | None = None) -> None:
        super().__init__(message)
        self.details = message
        self.artifacts = artifacts


class EventError(ExecutionError):
    """The durable event sink rejected an execution evidence event."""


@dataclass
class _ExecutionBudget:
    deadline: float
    timeout_s: float
    cancel: threading.Event
    artifacts: dict | None = None


_execution_budget: ContextVar[_ExecutionBudget | None] = ContextVar('factory_execution_budget', default=None)


def _remaining_budget() -> float | None:
    budget = _execution_budget.get()
    if budget is None:
        return None
    left = budget.deadline - time.monotonic()
    if left <= 0:
        budget.cancel.set()
        raise ExecutionError(f'execution timeout after {budget.timeout_s}s', artifacts=budget.artifacts)
    return left


def _deadline_checked(fn):
    """Checkpoint local inspection helpers whose syscalls are not cancellable."""
    @wraps(fn)
    def checked(*args, **kwargs):
        _remaining_budget()
        result = fn(*args, **kwargs)
        _remaining_budget()
        return result
    return checked


def _with_execution_budget(fn):
    @wraps(fn)
    def bounded(*args, **kwargs):
        timeout = kwargs.get('timeout_s', 14400)
        budget = _ExecutionBudget(time.monotonic() + float(timeout), timeout, kwargs['cancel'])
        token = _execution_budget.set(budget)
        try:
            result = fn(*args, **kwargs)
            _remaining_budget()
            return result
        finally:
            _execution_budget.reset(token)
    return bounded


def _reported_cost(value: Any) -> float | None:
    """Malformed usage is unknown, never a zero-cost or non-finite success."""
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _reported_tokens(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return tokens if tokens >= 0 else None


def _clip(value: Any, limit: int = _MAX_OUTPUT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: max(0, limit - 4)] + "\n..."


def _emit(emit: Callable[[str, dict, str | None], None], typ: str, payload: dict, task_id: str | None = None) -> None:
    # Event persistence is part of the execution evidence.  If it fails, stop
    # before claiming a task was verified; callers can persist the exception.
    try:
        emit(typ, payload, task_id)
    except Exception as exc:
        raise EventError(f"event emission failed for {typ}") from exc


def _git(root: Path, *argv: str, timeout_s: float) -> tuple[int, str, str]:
    remaining = _remaining_budget()
    timeout = float(timeout_s) if remaining is None else min(float(timeout_s), remaining)
    try:
        proc = run_bounded(
            ["git", *argv], cwd=root, timeout_s=max(0.001, timeout),
            env={**check_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except ProcTimeout as exc:
        _remaining_budget()
        raise ExecutionError(f"git timeout after {timeout_s}s: git {' '.join(argv)}") from exc
    _remaining_budget()
    return proc.returncode, proc.stdout, proc.stderr


def _git_ok(root: Path, *argv: str, timeout_s: float) -> str:
    rc, out, err = _git(root, *argv, timeout_s=timeout_s)
    if rc != 0:
        raise ExecutionError(f"git failed ({rc}): git {' '.join(argv)}: {_clip(err)}")
    return out.strip()


def _safe_ref_part(value: Any, label: str) -> str:
    value = str(value)
    if not _SAFE_REF.fullmatch(value) or value in {".", ".."}:
        raise ExecutionError(f"unsafe {label}: {value!r}")
    return value


def _relative(path: str) -> str:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts or ".git" in p.parts:
        raise ExecutionError(f"unsafe task path: {path!r}")
    return p.as_posix()


def _is_generated(path: str) -> bool:
    p = PurePosixPath(path)
    return any(part in _GENERATED_PARTS for part in p.parts) or p.name in _GENERATED_NAMES


def _status_paths(root: Path, *, timeout_s: float) -> tuple[str, ...]:
    # --ignored is intentional: ignored source files are still executable by a
    # check.  Cache names are filtered below, while shadow_code catches ignored
    # source files and turns them into a hard failure.
    rc, out, err = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored", timeout_s=timeout_s)
    if rc != 0:
        raise ExecutionError(f"cannot inspect worktree: {_clip(err)}")
    paths: set[str] = set()
    fields = out.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        xy, value = field[:2], field[3:]
        # With -z, rename/copy records carry the destination in the following
        # NUL record; the first record is the old path.
        if xy[0] in "RC" or xy[1] in "RC":
            paths.add(value)
            if index < len(fields) and fields[index]:
                paths.add(fields[index])
                index += 1
        else:
            paths.add(value)
    return tuple(sorted(p for p in paths if p and not _is_generated(p)))


@_deadline_checked
def _working_hash(root: Path, paths: Iterable[str], *, timeout_s: float) -> str:
    """Hash the complete visible change, including untracked files."""
    rc, diff, err = _git(root, "diff", "--no-ext-diff", "--binary", "HEAD", "--", timeout_s=timeout_s)
    if rc != 0:
        raise ExecutionError(f"cannot capture diff: {_clip(err)}")
    h = hashlib.sha256(diff.encode())
    for rel in sorted(set(paths)):
        file = root / rel
        if file.is_symlink():
            target = file.resolve()
            if not target.is_relative_to(root.resolve()):
                raise ExecutionError(f"changed symlink escapes workspace: {rel}")
            raise ExecutionError(f"symlink changes are not allowed: {rel}")
        h.update(rel.encode())
        if file.is_file():
            try:
                h.update(file.read_bytes())
            except OSError as exc:
                raise ExecutionError(f"cannot read changed path {rel}: {exc}") from exc
    return h.hexdigest() if diff or tuple(paths) else ""


def _reject_symlinks(root: Path, paths: Iterable[str]) -> None:
    for rel in paths:
        file = root / rel
        if file.is_symlink():
            target = file.resolve()
            if not target.is_relative_to(root.resolve()):
                raise ExecutionError(f"changed symlink escapes workspace: {rel}")
            raise ExecutionError(f"symlink changes are not allowed: {rel}")


@_deadline_checked
def _baseline(root: Path) -> dict[str, Any]:
    return {
        "head": head_position(root),
        "config": git_config(root),
        "hooks": __import__("factory.harness.workspace", fromlist=["hook_fingerprint"]).hook_fingerprint(root),
        "replace": replace_refs(root),
        "gitlinks": staged_gitlinks(root),
        "skipped": index_skipped(root),
        "attrs": info_attributes(root),
    }


@_deadline_checked
def _guard_workspace(root: Path, before: dict[str, Any], changed: tuple[str, ...]) -> None:
    now = _baseline(root)
    if now["head"] != before["head"]:
        raise ExecutionError("worker changed HEAD or its symbolic ref")
    if changed_config(before["config"], now["config"]):
        raise ExecutionError("worker changed Git config")
    if changed_hooks(before["hooks"], now["hooks"]):
        raise ExecutionError("worker changed Git hooks")
    if now["replace"] != before["replace"]:
        raise ExecutionError("worker changed Git replace refs")
    if now["gitlinks"] != before["gitlinks"]:
        raise ExecutionError("worker changed staged gitlinks")
    if newly_skipped(before["skipped"], now["skipped"]):
        raise ExecutionError("worker changed index skip flags")
    if now["attrs"] != before["attrs"]:
        raise ExecutionError("worker changed .git/info/attributes")
    if diff_suppressed(root, changed):
        raise ExecutionError("worker suppressed diff attributes")
    shadows = tuple(p for p in shadow_code(root) if not _is_generated(p))
    if shadows:
        raise ExecutionError(f"worker created ignored source: {', '.join(shadows[:8])}")


def _within(path: str, declared: tuple[str, ...]) -> bool:
    return any(path == d or path.startswith(d.rstrip("/") + "/") for d in declared)


def _overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(_within(a, right) or _within(b, left) for a in left for b in right)


def _check_argv(project: Mapping[str, Any], names: Iterable[Any]) -> list[tuple[str, list[str]]]:
    trusted = project.get("checks") or {}
    if not isinstance(trusted, Mapping):
        raise ExecutionError("project checks must be a mapping")
    out: list[tuple[str, list[str]]] = []
    for raw in names:
        name = str(raw)
        argv = trusted.get(name)
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ExecutionError(f"check {name!r} is not a trusted argv array")
        if any("\x00" in x for x in argv):
            raise ExecutionError(f"check {name!r} contains NUL")
        out.append((name, list(argv)))
    return out


def _run_check(root: Path, name: str, argv: list[str], timeout_s: float, emit: Callable, task_id: str | None, cancel: threading.Event | None = None) -> dict:
    _emit(emit, "tool/check", {"name": name, "argv": argv}, task_id)
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=root, env=check_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    out = err = ""
    timed_out = False
    cancelled = False
    deadline = started + max(0.1, timeout_s)
    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
        elif time.monotonic() >= deadline:
            timed_out = True
        if cancelled or timed_out:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            # Repeated communicate calls both drain large pipes and give us a
            # cancellation/deadline checkpoint while the process is running.
            out, err = proc.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if not (cancelled or timed_out):
                continue
            break
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
    if cancelled:
        record = {"name": name, "argv": argv, "exit": None, "cancelled": True, "stdout": _clip(out), "stderr": _clip(err), "duration_s": round(time.monotonic() - started, 3)}
    elif timed_out:
        record = {"name": name, "argv": argv, "exit": None, "timeout": True, "stdout": _clip(out), "stderr": _clip(err) or f"timeout after {timeout_s}s", "duration_s": round(time.monotonic() - started, 3)}
    else:
        record = {"name": name, "argv": argv, "exit": proc.returncode, "stdout": _clip(out), "stderr": _clip(err), "duration_s": round(time.monotonic() - started, 3)}
    _emit(emit, "check.result", record, task_id)
    return record


@_deadline_checked
def _commit_tree(root: Path, paths: tuple[str, ...], message: str, timeout_s: float) -> str:
    if not paths:
        raise ExecutionError("worker produced no changes")
    rc, _, err = _git(root, "add", "-A", "--", *paths, timeout_s=timeout_s)
    if rc != 0:
        raise ExecutionError(f"git add failed: {_clip(err)}")
    staged = tuple(x for x in _git_ok(root, "diff", "--cached", "--name-only", "-z", timeout_s=timeout_s).split("\0") if x)
    if set(staged) != set(paths):
        raise ExecutionError("git staging included paths outside the verified change")
    staged_tree = _git_ok(root, "write-tree", timeout_s=timeout_s)
    rc, _, err = _git(root, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Factory", "-c", "user.email=factory@localhost", "commit", "-m", message, "--no-edit", timeout_s=timeout_s)
    if rc != 0:
        raise ExecutionError(f"git commit failed: {_clip(err)}")
    commit = _git_ok(root, "rev-parse", "HEAD", timeout_s=timeout_s)
    committed_tree = _git_ok(root, "rev-parse", "HEAD^{tree}", timeout_s=timeout_s)
    if committed_tree != staged_tree:
        raise ExecutionError("committed tree differs from verified staged tree")
    return commit


def _protected_changes(root: Path, changed: tuple[str, ...]) -> tuple[str, ...]:
    """Project/package metadata is editable; test selection and hooks remain frozen."""
    blocked = list(judge_files_touched(changed))
    if 'pyproject.toml' not in blocked:
        return tuple(blocked)
    try:
        rc, original, _ = _git(root, 'show', 'HEAD:pyproject.toml', timeout_s=30)
        before = tomllib.loads(original) if rc == 0 else {}
        after = tomllib.loads((root / 'pyproject.toml').read_text())
        def protected(document):
            document = dict(document)
            project = dict(document.get('project', {}))
            for key in ('name', 'version', 'description', 'readme', 'requires-python', 'license', 'license-files', 'authors', 'maintainers', 'keywords', 'classifiers', 'urls', 'dependencies', 'optional-dependencies'):
                project.pop(key, None)
            # Entry-point plugins and custom dynamic metadata stay protected.
            scripts = dict(project.get('scripts', {}))
            scripts = {key: value for key, value in scripts.items() if key in ('pytest', 'py.test', 'python', 'pip')}
            if scripts:
                project['scripts'] = scripts
            else:
                project.pop('scripts', None)
            if project:
                document['project'] = project
            else:
                document.pop('project', None)
            # Standard setuptools packaging is normal scaffolding. Custom
            # backend paths and alternative backends remain review boundaries.
            build = document.get('build-system', {})
            if set(build) <= {'requires', 'build-backend'} and build.get('build-backend') == 'setuptools.build_meta' and all(re.fullmatch(r'(setuptools|wheel)([<>=!~].*)?', item) for item in build.get('requires', [])):
                document.pop('build-system', None)
            tool = dict(document.get('tool', {}))
            packaging = dict(tool.get('setuptools', {}))
            for key in ('packages', 'package-dir', 'package-data', 'exclude-package-data', 'include-package-data', 'py-modules'):
                packaging.pop(key, None)
            if packaging:
                tool['setuptools'] = packaging
            else:
                tool.pop('setuptools', None)
            if tool:
                document['tool'] = tool
            else:
                document.pop('tool', None)
            return document
        if protected(before) == protected(after):
            blocked.remove('pyproject.toml')
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return tuple(blocked)


def _restore_draft(workspace: Path, prior: dict, target: Path, timeout_s: float) -> None:
    """Copy only file changes into a fresh guarded worktree; never copy Git metadata."""
    source = Path(prior.get('worktree') or '').resolve()
    if not source.is_dir() or not source.is_relative_to(workspace.parent):
        raise ExecutionError('paused task workspace is missing; cannot continue its draft')
    expected = _git_ok(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout_s=timeout_s)
    actual = _git_ok(source, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout_s=timeout_s)
    if actual != expected or _git_ok(source, 'symbolic-ref', '--short', 'HEAD', timeout_s=timeout_s) != prior.get('branch'):
        raise ExecutionError('paused task workspace identity changed')
    changed = _status_paths(source, timeout_s=timeout_s)
    for rel in changed:
        _relative(rel)
        file = source / rel
        if not file.resolve().is_relative_to(source) or any(part in _FORBIDDEN_PARTS for part in PurePosixPath(rel).parts) or PurePosixPath(rel).name in _FORBIDDEN_NAMES:
            raise ExecutionError('paused draft contains forbidden paths')
    _reject_symlinks(source, changed)
    patch = _git_ok(source, 'diff', '--no-ext-diff', '--binary', 'HEAD', '--', timeout_s=timeout_s)
    if patch:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.patch') as output:
            output.write(patch); output.flush()
            _git_ok(target, 'apply', '--binary', '--', output.name, timeout_s=timeout_s)
    untracked = _git_ok(source, 'ls-files', '--others', '--exclude-standard', '-z', timeout_s=timeout_s).split('\0')
    for rel in untracked:
        if not rel or rel not in changed:
            continue
        file, destination = source / rel, target / rel
        if not destination.resolve().is_relative_to(target.resolve()) or destination.exists():
            raise ExecutionError('paused draft conflicts with completed work')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)


@_with_execution_budget
def execute_plan(
    *,
    run_id: str,
    plan: dict,
    project: dict,
    profiles: dict,
    runner: Any,
    emit: Callable[[str, dict, str | None], None],
    cancel: threading.Event,
    max_parallel: int = 2,
    timeout_s: int = 14400,
    resume_artifacts: dict | None = None,
) -> dict:
    """Execute and integrate a validated plan, preserving every worktree."""
    if max_parallel < 1:
        raise ExecutionError("max_parallel must be positive")
    if timeout_s <= 0:
        raise ExecutionError("timeout_s must be positive")
    def remaining() -> float:
        return _remaining_budget()

    run_part = _safe_ref_part(run_id, "run id")
    workspace = Path(project.get("workspace", "")).expanduser().resolve()
    if not workspace.is_dir():
        raise ExecutionError(f"project workspace does not exist: {workspace}")
    base_branch = project.get("base_branch")
    if not isinstance(base_branch, str) or not base_branch or base_branch.startswith("-"):
        raise ExecutionError("project base_branch is required")
    base_sha = _git_ok(workspace, "rev-parse", "--verify", f"{base_branch}^{{commit}}", timeout_s=timeout_s)
    if project.get('expected_base_sha') and project['expected_base_sha'] != base_sha:
        raise ExecutionError('project baseline changed since planning; review and replan before execution')
    raw_tasks = plan.get("tasks") if isinstance(plan, Mapping) else None
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ExecutionError("plan has no tasks")
    max_tasks = project.get('max_tasks', 20)
    if type(max_tasks) is not int or not 1 <= max_tasks <= 20 or len(raw_tasks) > max_tasks:
        raise ExecutionError('plan exceeds the configured task limit')
    unknown_cost_policy = project.get('unknown_cost_policy', 'stop')
    if unknown_cost_policy not in ('stop', 'allow_bounded'):
        raise ExecutionError('invalid unknown_cost_policy')

    tasks: dict[str, dict] = {}
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise ExecutionError("task must be an object")
        task_id = _safe_ref_part(raw.get("id", ""), "task id")
        if task_id in tasks:
            raise ExecutionError(f"duplicate task id: {task_id}")
        paths = tuple(_relative(str(p)) for p in (raw.get("paths") or ()))
        if not paths:
            raise ExecutionError(f"task {task_id} has no paths")
        if not isinstance(raw.get("checks"), (list, tuple)) or not raw.get("checks"):
            raise ExecutionError(f"task {task_id} has no checks")
        for path in paths:
            pp = PurePosixPath(path)
            if any(part in _FORBIDDEN_PARTS for part in pp.parts) or pp.name in _FORBIDDEN_NAMES:
                raise ExecutionError(f"task {task_id} has forbidden path: {path}")
        deps = tuple(str(d) for d in (raw.get("depends_on") or ()))
        tasks[task_id] = {**raw, "id": task_id, "paths": paths, "depends_on": deps}
    for task in tasks.values():
        if any(dep not in tasks or dep == task["id"] for dep in task["depends_on"]):
            raise ExecutionError(f"invalid dependency for task {task['id']}")
    # Detect cycles before creating a worktree.
    def visit(node: str, stack: set[str], seen: set[str]) -> None:
        if node in stack:
            raise ExecutionError("plan dependency cycle")
        if node in seen:
            return
        stack.add(node)
        for dep in tasks[node]["depends_on"]:
            visit(dep, stack, seen)
        stack.remove(node)
        seen.add(node)
    seen: set[str] = set()
    for task_id in tasks:
        visit(task_id, set(), seen)

    # Git refs cannot have both ``factory/run`` and ``factory/run/task`` (the
    # former is a file where the latter needs a directory).  The delivery ref
    # therefore keeps the contract's exact ``factory/<runid>`` spelling while
    # child attempt refs use a sibling namespace.
    integration_branch = f"factory/{run_part}"
    root = Path(tempfile.mkdtemp(prefix=f".factory-{run_part}-", dir=str(workspace.parent)))
    integration_path = root / "integration"
    _git_ok(workspace, "worktree", "add", "-q", "-b", integration_branch, str(integration_path), base_sha, timeout_s=timeout_s)
    artifacts: dict[str, Any] = {"branch": integration_branch, "base_sha": base_sha, "commit": None, "worktree": str(integration_path), "checks": [], "tasks": []}
    _execution_budget.get().artifacts = artifacts
    by_id = {task_id: {"id": task_id, "status": "queued", "profile": None, "branch": None, "worktree": None, "commit": None, "checks": [], "attempts": []} for task_id in tasks}
    artifacts['tasks'] = list(by_id.values())
    child_commits: dict[str, str] = {}
    integrated = base_sha
    previous = {item['id']: item for item in (resume_artifacts or {}).get('tasks', []) if item.get('id') in tasks}
    restored: set[str] = set()
    if resume_artifacts:
        if resume_artifacts.get('base_sha') != base_sha:
            raise ExecutionError('paused execution baseline changed', artifacts=artifacts)
        pending_verified = {key for key, value in previous.items() if value.get('status') in ('verified', 'completed') and value.get('commit')}
        while pending_verified:
            ready_verified = [key for key in tasks if key in pending_verified and all(dep in restored for dep in tasks[key]['depends_on'])]
            if not ready_verified:
                raise ExecutionError('paused verified task dependencies are incomplete', artifacts=artifacts)
            for key in ready_verified:
                commit = previous[key]['commit']
                if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
                    raise ExecutionError('invalid paused task commit', artifacts=artifacts)
                _git_ok(integration_path, '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Factory', '-c', 'user.email=factory@localhost', 'cherry-pick', commit, timeout_s=timeout_s)
                by_id[key] = dict(previous[key])
                restored.add(key); pending_verified.remove(key)
        integrated = _git_ok(integration_path, 'rev-parse', 'HEAD', timeout_s=timeout_s)
        artifacts['tasks'] = list(by_id.values())
    observed_cost = 0.0
    cost_unknown = False
    cost_lock = threading.Lock()
    budget = project.get("budget_usd")
    try:
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        raise ExecutionError("project budget_usd must be numeric") from None

    routing_policy = project.get("routing_policy")
    if routing_policy is None:
        # v2 callers did not authorize retries. Preserve their one-call
        # execution behavior even when profiles contain stronger roles.
        max_attempts, auto_escalate = 1, False
    else:
        if not isinstance(routing_policy, Mapping):
            raise ExecutionError("routing_policy must be a mapping")
        max_attempts = routing_policy.get("max_attempts")
        auto_escalate = routing_policy.get("auto_escalate")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ExecutionError("routing_policy max_attempts must be between 1 and 3")
        if not isinstance(auto_escalate, bool):
            raise ExecutionError("routing_policy auto_escalate must be a boolean")

    def checkpoint() -> None:
        with cost_lock:
            known_cost = observed_cost
        _emit(emit, "execution.checkpoint", {
            "integration_branch": integration_branch,
            "integration_worktree": str(integration_path),
            "base_sha": base_sha,
            "current_commit": integrated,
            "tasks": list(by_id.values()),
            "known_cost_usd": known_cost,
        })

    def dispatch_block_reason() -> str | None:
        """Read the attempt ledger before a new provider call is dispatched."""
        with cost_lock:
            if cost_unknown and unknown_cost_policy == "stop":
                return "provider cost is unknown; continuation requires human review"
            if budget is not None and observed_cost >= budget:
                return f"known provider cost ${observed_cost:.4f} exhausted budget ${budget:.4f}"
        return None

    checkpoint()

    def run_one(task: dict, child_root: Path, branch: str, route: Mapping[str, Any]) -> dict:
        task_id = task["id"]
        before = _baseline(child_root)
        _emit(emit, "model.selected", dict(route), task_id)
        _emit(emit, "attempt.started", {**route, "branch": branch, "worktree": str(child_root)}, task_id)
        result = None
        cost: float | None = None
        usage_emitted = False
        streamed_usage: dict[str, Any] = {}
        # GovernedRunner emits quota.reserved only after its atomic token
        # reservation.  Do not create usage evidence for a provider call until
        # that point: an exhausted quota must leave no phantom unknown charge.
        provider_dispatched = getattr(runner, "governance", None) is None
        provider_started_emitted = False

        def provider_started(call_id: Any = None) -> None:
            nonlocal provider_started_emitted
            if provider_started_emitted:
                return
            provider_started_emitted = True
            payload = {**route}
            if isinstance(call_id, str) and call_id:
                payload["call_id"] = call_id
            _emit(emit, "provider.started", payload, task_id)

        def remember_streamed_usage(payload: Any) -> None:
            if not isinstance(payload, Mapping):
                return
            total = payload.get("total")
            source = total if isinstance(total, Mapping) else payload
            for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
                value = _reported_tokens(source.get(name))
                if value is not None:
                    streamed_usage[name] = value
            value = _reported_cost(source.get("cost_usd"))
            if value is not None:
                streamed_usage["cost_usd"] = value

        def usage() -> None:
            nonlocal usage_emitted, cost, observed_cost, cost_unknown
            if usage_emitted or not provider_dispatched:
                return
            usage_emitted = True
            cost = _reported_cost(getattr(result, "cost_usd", None)) if result is not None else None
            if cost is None:
                cost = _reported_cost(streamed_usage.get("cost_usd"))
            payload: dict[str, Any] = {**route, "cost_usd": cost}
            for source, target in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens"),
                                   ("cached_input_tokens", "cached_input_tokens")):
                value = getattr(result, source, None) if result is not None else None
                if value is None:
                    value = streamed_usage.get(target)
                if value is not None:
                    payload[target] = value
            _emit(emit, "usage.recorded", payload, task_id)
            # The event is durable before its charge enters the shared dispatch
            # ledger. Threads may already be in flight, but no later attempt
            # or wave may start once the configured budget/unknown-cost policy
            # is reached.
            with cost_lock:
                if cost is None:
                    cost_unknown = True
                    artifacts['billing_incomplete'] = 'provider cost is unknown; the dollar budget cannot be guaranteed'
                    artifacts['autopublish_blocked'] = True
                    artifacts['known_cost_usd'] = observed_cost
                    artifacts['observed_cost_usd'] = None
                    return
                subtotal = observed_cost + cost
                if not math.isfinite(subtotal):
                    artifacts.update(billing_incomplete='reported cost subtotal overflowed; the dollar budget cannot be verified',
                                     autopublish_blocked=True, observed_cost_usd=None,
                                     known_cost_usd=observed_cost)
                    raise ExecutionError('reported cost subtotal overflowed', artifacts=artifacts)
                observed_cost = subtotal
                artifacts['known_cost_usd'] = observed_cost
                artifacts['observed_cost_usd'] = None if cost_unknown else observed_cost

        def finish(status: str, **values: Any) -> dict:
            usage()
            payload = {**route, "status": status, "cost_usd": cost, **values}
            _emit(emit, "attempt.completed" if status == "verified" else "attempt.failed", payload, task_id)
            return {"status": status, "cost_usd": cost, "attempt": payload, **values}

        try:
            from factory.control.providers import ProviderRequest
            request = ProviderRequest(provider=str(route["provider"]), model=str(route["model"]), prompt=str(task.get("prompt", "")), workspace=str(child_root), timeout_s=max(1, int(remaining())), read_only=False)
            last_assistant_text: str | None = None
            def callback(typ: Any, payload: Any = None, *extra: Any) -> None:
                # SDK adapters historically used both emit(kind, payload) and
                # emit(kind, payload, task_id); task identity is coordinator
                # owned, so ignore a provider supplied third argument.
                nonlocal last_assistant_text
                nonlocal provider_dispatched
                kind = str(typ)
                if kind in {"assistant.message", "assistant_message"}:
                    if isinstance(payload, dict):
                        candidate = payload.get("text", payload.get("content"))
                    else:
                        candidate = payload
                    if candidate is not None:
                        last_assistant_text = str(candidate)
                if kind == "provider.usage":
                    remember_streamed_usage(payload)
                event_payload = payload if isinstance(payload, dict) else {"value": payload}
                _emit(emit, kind, event_payload, task_id)
                if kind == "quota.reserved":
                    provider_dispatched = True
                    provider_started(event_payload.get("id"))
            if provider_dispatched:
                provider_started()
            result = runner.run(request, callback, cancel=cancel)
            final_text = str(getattr(result, "text", "") or "")
            if final_text and final_text != last_assistant_text:
                _emit(emit, "assistant.message", {"text": final_text}, task_id)
            usage()
            if cancel.is_set():
                return finish("cancelled", error="cancel requested", retryable=False)
            changed = _status_paths(child_root, timeout_s=timeout_s)
            if any(any(part in _FORBIDDEN_PARTS for part in PurePosixPath(p).parts) or PurePosixPath(p).name in _FORBIDDEN_NAMES for p in changed):
                raise ExecutionError("worker changed forbidden metadata or secret path")
            declared = tuple(task["paths"])
            out_of_scope = tuple(p for p in changed if not _within(p, declared))
            if out_of_scope:
                raise ExecutionError(f"out-of-scope changes: {', '.join(out_of_scope)}")
            _reject_symlinks(child_root, changed)
            judge = _protected_changes(child_root, changed)
            hooks = runner_hooks(child_root)
            if judge or hooks:
                raise ExecutionError(f"worker changed test/check infrastructure: {', '.join(judge + hooks)}")
            _guard_workspace(child_root, before, changed)
            before_hash = _working_hash(child_root, changed, timeout_s=timeout_s)
            records: list[dict] = []
            for name, argv in _check_argv(project, task.get("checks") or ()):
                record = _run_check(child_root, name, argv, remaining(), emit, task_id, cancel)
                records.append(record)
                if record.get("cancelled"):
                    return finish("cancelled", checks=records, error="cancel requested", retryable=False)
                if record.get("timeout") or record.get("exit") != 0:
                    return finish("failed", checks=records, error=f"verification failed: {name}", retryable=True)
            after_changed = _status_paths(child_root, timeout_s=timeout_s)
            _reject_symlinks(child_root, after_changed)
            _guard_workspace(child_root, before, after_changed)
            after_hash = _working_hash(child_root, after_changed, timeout_s=timeout_s)
            if before_hash != after_hash or tuple(after_changed) != tuple(changed):
                raise ExecutionError("verification changed source files")
            commit = _commit_tree(child_root, changed, f"factory: {task_id}", timeout_s)
            if _status_paths(child_root, timeout_s=timeout_s):
                raise ExecutionError("worktree dirty after commit")
            committed_diff = _git_ok(child_root, "show", "--format=", "--binary", commit, timeout_s=timeout_s)
            commit_hash = diff_hash(committed_diff)
            _emit(emit, "git.commit", {"commit": commit, "branch": branch, "diff_hash": commit_hash}, task_id)
            return finish("verified", commit=commit, diff_hash=commit_hash, checks=records, retryable=False)
        except EventError:
            raise
        except ExecutionError as exc:
            usage()
            if str(exc) == 'reported cost subtotal overflowed':
                _emit(emit, "attempt.failed", {**route, "status": "failed", "cost_usd": cost,
                      "error": str(exc), "retryable": False, "failure_kind": "billing"}, task_id)
                raise
            # Scope/metadata guards, cancellation and the shared deadline are
            # control-plane boundaries. A new model cannot be allowed to evade
            # them. A no-change response is the narrowly repairable exception.
            retryable = str(exc) == "worker produced no changes"
            return finish("failed", error=str(exc), retryable=retryable,
                          failure_kind="execution" if retryable else "policy_or_deadline")
        except Exception as exc:
            usage()
            return finish("failed", error=f"worker error: {type(exc).__name__}: {_clip(exc)}", retryable=True,
                          failure_kind="execution")

    def run_with_retries(task: dict, child_root: Path, branch: str, route: Mapping[str, Any], *, attempt_limit: int | None = None) -> dict:
        """Keep failed attempts and repair evidence in separate worktrees."""
        from factory.control.model_routing import RoutingError, select_profile

        task_id = task["id"]
        limit = max_attempts if attempt_limit is None else attempt_limit
        attempts: list[dict[str, Any]] = []
        current_task, current_root, current_branch, current_route = task, child_root, branch, route
        while True:
            result = run_one(current_task, current_root, current_branch, current_route)
            attempt_evidence = result.pop("attempt")
            attempts.append(attempt_evidence)
            if result.get("status") == "verified":
                total_cost = sum(cost for cost in (item.get("cost_usd") for item in attempts) if cost is not None)
                task_cost = None if any(item.get("cost_usd") is None for item in attempts) else total_cost
                return {**result, "cost_usd": task_cost, "known_cost_usd": total_cost,
                        "attempts": attempts, "profile": current_route["profile"],
                        "branch": current_branch, "worktree": str(current_root)}
            if (not result.get("retryable") or len(attempts) >= limit or cancel.is_set()):
                total_cost = sum(cost for cost in (item.get("cost_usd") for item in attempts) if cost is not None)
                task_cost = None if any(item.get("cost_usd") is None for item in attempts) else total_cost
                return {**result, "cost_usd": task_cost, "known_cost_usd": total_cost,
                        "attempts": attempts, "profile": current_route["profile"],
                        "branch": current_branch, "worktree": str(current_root)}
            blocked = dispatch_block_reason()
            if blocked:
                known_attempt_cost = sum(cost for cost in (item.get("cost_usd") for item in attempts) if cost is not None)
                return {**result, "error": blocked,
                        "retryable": False, "cost_usd": None if any(item.get("cost_usd") is None for item in attempts) else known_attempt_cost,
                        "known_cost_usd": known_attempt_cost, "attempts": attempts,
                        "profile": current_route["profile"], "branch": current_branch,
                        "worktree": str(current_root)}
            _remaining_budget()
            next_attempt = len(attempts) + 1
            try:
                next_route = select_profile(task, profiles, attempt=next_attempt, auto_escalate=auto_escalate)
            except RoutingError as exc:
                # A missing upgrade profile is configuration evidence, not a
                # reason to silently retry an unrelated or weaker model.
                return {"status": "failed", "error": str(exc), "retryable": False,
                        "attempts": attempts, "profile": current_route["profile"],
                        "branch": current_branch, "worktree": str(current_root)}
            evidence = _clip(result.get("error", ""), 4_000)
            current_task = {**task, "prompt": str(task.get("prompt", "")) +
                            "\n\nPrevious attempt failed. Repair only the declared scope and preserve trusted checks.\n"
                            f"Failure evidence: {evidence}"}
            current_branch = f"factory/{run_part}-attempt/{task_id}/{next_attempt}"
            current_root = root / f"task-{task_id}-a{next_attempt}"
            _git_ok(workspace, "worktree", "add", "-q", "-b", current_branch, str(current_root),
                    integration_branch, timeout_s=timeout_s)
            current_route = next_route

    completed: set[str] = set(restored)
    failed: str | None = None
    cancelled = False
    stop_reason: str | None = None
    stop_kind: str | None = None
    stop_blocked = False
    while len(completed) < len(tasks):
        remaining()
        if cancel.is_set():
            for task_id in tasks:
                if task_id not in completed:
                    by_id[task_id]["status"] = "cancelled"
            raise ExecutionError("execution cancelled", artifacts=artifacts)
        current_block = dispatch_block_reason()
        if current_block is not None:
            stop_reason = current_block
            stop_kind = "unknown_cost" if cost_unknown else "budget"
        ready = [task for task_id, task in tasks.items() if task_id not in completed and all(dep in completed for dep in task["depends_on"])]
        if failed is not None:
            for task in tasks.values():
                if task["id"] not in completed:
                    by_id[task["id"]]["status"] = "blocked"
            raise ExecutionError(f"task {failed} failed", artifacts={**artifacts, "tasks": list(by_id.values())})
        if stop_reason is not None:
            for task in tasks.values():
                if task["id"] not in completed:
                    by_id[task["id"]]["status"] = "blocked"
                    stop_blocked = True
            break
        if not ready:
            raise ExecutionError("DAG made no progress", artifacts=artifacts)
        # Overlapping paths are serialized within a wave.  Independent tasks get
        # distinct worktrees and are safe to run together.
        wave: list[dict] = []
        used: set[str] = set()
        for task in ready:
            if len(wave) >= max_parallel or any(_overlaps(tuple(task["paths"]), tuple(other["paths"])) for other in wave):
                continue
            wave.append(task)
            used.update(task["paths"])
        if not wave:
            wave = [ready[0]]
        futures: dict[concurrent.futures.Future, tuple[dict, Path, str, Mapping[str, Any]]] = {}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="factory-task")
        pending: set[concurrent.futures.Future] = set()
        try:
            for task in wave:
                remaining()
                task_id = task["id"]
                from factory.control.model_routing import RoutingError, select_profile
                try:
                    route = select_profile(task, profiles, attempt=1, auto_escalate=auto_escalate)
                except RoutingError as exc:
                    raise ExecutionError(str(exc), artifacts=artifacts) from None
                child_branch = f"factory/{run_part}-task/{task_id}"
                child_path = root / f"task-{task_id}"
                _git_ok(workspace, "worktree", "add", "-q", "-b", child_branch, str(child_path), integration_branch, timeout_s=timeout_s)
                prior = previous.get(task_id)
                if prior and prior.get('status') == 'failed' and prior.get('worktree'):
                    _restore_draft(workspace, prior, child_path, timeout_s)
                    task = {**task, 'prompt': str(task.get('prompt', '')) +
                            '\n\nContinue the existing draft already present in this workspace. Do not redo completed tasks. Repair the failure while preserving the original declared paths and trusted checks. Python project metadata may be edited, but restore test-selection/configuration changes to their original values.\nFailure evidence: ' + str(prior.get('error') or (prior.get('attempts') or [{}])[-1].get('error', ''))}
                    _emit(emit, 'task.draft_restored', {'previous_worktree': prior['worktree'], 'worktree': str(child_path)}, task_id)
                by_id[task_id].update({"profile": route["profile"], "branch": child_branch, "worktree": str(child_path), "status": "running"})
                _emit(emit, "task.started", {**route, "branch": child_branch, "worktree": str(child_path)}, task_id)
                future = pool.submit(copy_context().run, run_with_retries, task, child_path, child_branch, route)
                futures[future] = (task, child_path, child_branch, route)
            done, pending = concurrent.futures.wait(futures, timeout=remaining())
            if pending:
                cancel.set()
                raise ExecutionError(f"task timeout after {timeout_s}s", artifacts=artifacts)
            for future in done:
                task, child_path, child_branch, _ = futures[future]
                task_id = task["id"]
                result = future.result()
                previous_attempts = previous.get(task_id, {}).get('attempts', [])
                if previous_attempts:
                    result['attempts'] = [*previous_attempts, *[{**attempt, 'attempt': len(previous_attempts) + index + 1} for index, attempt in enumerate(result.get('attempts', []))]]
                by_id[task_id].update(result)
                completed.add(task_id)
                if result.get("status") == "verified":
                    _emit(emit, "task.completed", {"status": "verified", "commit": result.get("commit"), "cost_usd": result.get("cost_usd"), "attempts": result.get("attempts", [])}, task_id)
                else:
                    _emit(emit, "task.failed", {"status": result.get("status", "failed"), "error": result.get("error", ""), "attempts": result.get("attempts", [])}, task_id)
                attempt_costs = [attempt.get("cost_usd") for attempt in result.get("attempts", [])]
                if any(cost is None for cost in attempt_costs):
                    artifacts['billing_incomplete'] = 'provider cost is unknown; the dollar budget cannot be guaranteed'
                    artifacts['autopublish_blocked'] = True
                    _emit(emit, 'billing.unknown', {'policy': unknown_cost_policy,
                          'message': artifacts['billing_incomplete']}, task_id)
                    if unknown_cost_policy == 'stop' and stop_kind != 'budget':
                        stop_reason = "provider cost is unknown; continuation requires human review"
                        stop_kind = "unknown_cost"
                current_block = dispatch_block_reason()
                if current_block is not None:
                    stop_reason = current_block
                    stop_kind = "unknown_cost" if cost_unknown else "budget"
                if result.get("status") != "verified":
                    if result.get("status") == "cancelled":
                        cancelled = True
                    else:
                        failed = task_id
        finally:
            if pending:
                cancel.set()
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True, cancel_futures=True)
        if cancelled:
            for task in tasks.values():
                if task["id"] not in completed:
                    by_id[task["id"]]["status"] = "cancelled"
            raise ExecutionError("execution cancelled", artifacts={**artifacts, "tasks": list(by_id.values())})
        if failed is not None:
            continue
        # Integrate this completed wave serially, in plan order.
        for task in wave:
            task_id = task["id"]
            commit = by_id[task_id].get("commit")
            if not commit:
                continue
            rc, _, err = _git(integration_path, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Factory", "-c", "user.email=factory@localhost", "cherry-pick", commit, timeout_s=timeout_s)
            if rc != 0:
                _git(integration_path, "cherry-pick", "--abort", timeout_s=timeout_s)
                raise ExecutionError(f"cherry-pick failed for {task_id}: {_clip(err)}", artifacts={**artifacts, "tasks": list(by_id.values())})
            integrated = _git_ok(integration_path, "rev-parse", "HEAD", timeout_s=timeout_s)
        checkpoint()
        # Dependents must start from the newly integrated commit.

    if cancelled:
        raise ExecutionError("execution cancelled", artifacts={**artifacts, "tasks": list(by_id.values())})
    if failed is not None:
        for task in tasks.values():
            if task["id"] not in completed:
                by_id[task["id"]]["status"] = "blocked"
        raise ExecutionError(f"task {failed} failed", artifacts={**artifacts, "tasks": list(by_id.values())})

    final_checks: list[dict] = []
    integration_repaired = False
    while True:
        final_before = _baseline(integration_path)
        final_tree = _git_ok(integration_path, "rev-parse", "HEAD^{tree}", timeout_s=timeout_s)
        if _status_paths(integration_path, timeout_s=timeout_s):
            raise ExecutionError("integration worktree is dirty before final verification", artifacts={**artifacts, "tasks": list(by_id.values()), "commit": integrated})
        final_checks = []
        failed_check: tuple[str, dict] | None = None
        for name, argv in _check_argv(project, (project.get("checks") or {}).keys()):
            record = _run_check(integration_path, name, argv, remaining(), emit, None, cancel)
            final_checks.append(record)
            if record.get("cancelled"):
                raise ExecutionError("execution cancelled", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
            if record.get("timeout") or record.get("exit") != 0:
                failed_check = (name, record)
                break
        if failed_check is None:
            break
        # Only projects that explicitly opted into bounded routing receive one
        # repair pass.  It is a new, auditable task limited to the union of
        # already-authorized paths and trusted final checks.
        if integration_repaired or routing_policy is None or max_attempts <= 1:
            raise ExecutionError(f"final verification failed: {failed_check[0]}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
        blocked = dispatch_block_reason()
        if blocked:
            raise ExecutionError(f"final verification failed: {failed_check[0]}; integration repair not dispatched: {blocked}",
                                 artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
        repair_id = "integration-repair"
        if repair_id in by_id:
            raise ExecutionError("final verification failed and integration repair task id is unavailable", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
        evidence = _clip((str(failed_check[1].get("stderr", "")) + "\n" + str(failed_check[1].get("stdout", ""))).strip(), 4_000)
        repair_task = {"id": repair_id, "title": "Repair integrated delivery", "complexity": "large", "risk": "medium",
                       "paths": tuple(sorted({path for task in tasks.values() for path in task["paths"]})),
                       "checks": list((project.get("checks") or {}).keys()), "acceptance": [], "depends_on": [],
                       "prompt": "The integrated delivery failed a trusted final check. Repair only the declared paths; do not modify test or check infrastructure.\n"
                                 f"Failing check: {failed_check[0]}\nFailure evidence:\n{evidence}"}
        from factory.control.model_routing import RoutingError, select_profile
        try:
            route = select_profile(repair_task, profiles, attempt=1, auto_escalate=auto_escalate)
        except RoutingError as exc:
            raise ExecutionError(str(exc), artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated}) from None
        repair_branch = f"factory/{run_part}-task/{repair_id}"
        repair_path = root / f"task-{repair_id}"
        _git_ok(workspace, "worktree", "add", "-q", "-b", repair_branch, str(repair_path), integration_branch, timeout_s=timeout_s)
        by_id[repair_id] = {"id": repair_id, "status": "running", "profile": route["profile"], "branch": repair_branch, "worktree": str(repair_path), "commit": None, "checks": [], "attempts": []}
        _emit(emit, "task.started", {**route, "branch": repair_branch, "worktree": str(repair_path), "integration_repair": True}, repair_id)
        # A failed final check receives one provider repair attempt.  It is a
        # separate task for auditability, but must not inherit the regular
        # task escalation loop and turn one integration repair into several
        # paid model calls.
        result = run_with_retries(repair_task, repair_path, repair_branch, route, attempt_limit=1)
        by_id[repair_id].update(result)
        if result.get("status") != "verified":
            _emit(emit, "task.failed", {"status": result.get("status", "failed"), "error": result.get("error", ""), "attempts": result.get("attempts", []), "integration_repair": True}, repair_id)
            raise ExecutionError(f"integration repair failed: {result.get('error', 'unknown error')}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
        _emit(emit, "task.completed", {"status": "verified", "commit": result.get("commit"), "cost_usd": result.get("cost_usd"), "attempts": result.get("attempts", []), "integration_repair": True}, repair_id)
        rc, _, err = _git(integration_path, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Factory", "-c", "user.email=factory@localhost", "cherry-pick", str(result["commit"]), timeout_s=timeout_s)
        if rc != 0:
            _git(integration_path, "cherry-pick", "--abort", timeout_s=timeout_s)
            raise ExecutionError(f"cherry-pick failed for integration repair: {_clip(err)}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
        integrated = _git_ok(integration_path, "rev-parse", "HEAD", timeout_s=timeout_s)
        integration_repaired = True
        checkpoint()
    final_changed = _status_paths(integration_path, timeout_s=timeout_s)
    if final_changed:
        raise ExecutionError(f"final verification left worktree changes: {', '.join(final_changed)}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    if head_position(integration_path) != final_before["head"] or _git_ok(integration_path, "rev-parse", "HEAD^{tree}", timeout_s=timeout_s) != final_tree:
        raise ExecutionError("final verification changed integration HEAD or tree", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    _reject_symlinks(integration_path, final_changed)
    if judge_files_touched(final_changed) or runner_hooks(integration_path):
        raise ExecutionError("final verification changed test/check infrastructure", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    _guard_workspace(integration_path, final_before, final_changed)
    remaining()
    artifacts.update({"commit": integrated, "checks": final_checks, "tasks": list(by_id.values()),
                      "observed_cost_usd": None if cost_unknown else observed_cost,
                      "known_cost_usd": observed_cost,
                      "unknown_cost_policy": unknown_cost_policy})
    if stop_reason is not None:
        if stop_kind == "unknown_cost" and not stop_blocked:
            artifacts["billing_incomplete"] = stop_reason
        else:
            artifacts["needs_human"] = stop_reason
        if stop_kind == "budget":
            artifacts["autopublish_blocked"] = True
    if stop_blocked:
        raise ExecutionError(stop_reason or "continuation requires human review", artifacts=artifacts)
    return artifacts
