"""Bounded, evidence producing execution of a planned task DAG.

This module deliberately keeps the provider boundary small.  Providers edit an
isolated worktree; all Git operations, checks, scope validation, and integration
are performed here with argv based subprocesses.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
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
    try:
        proc = run_bounded(
            ["git", *argv], cwd=root, timeout_s=max(0.1, float(timeout_s)),
            env={**check_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except ProcTimeout as exc:
        raise ExecutionError(f"git timeout after {timeout_s}s: git {' '.join(argv)}") from exc
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


def _profile(task: Mapping[str, Any], profiles: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    # Profile selection is a control-plane policy.  Normalize paths to the
    # list shape required by planning.profile_for and ignore model overrides.
    from factory.control.planning import profile_for
    normalized = dict(task)
    normalized["paths"] = list(task.get("paths") or ())
    normalized["checks"] = list(task.get("checks") or ())
    name = str(profile_for(normalized))
    config = profiles.get(name)
    if not isinstance(config, Mapping):
        raise ExecutionError(f"profile {name!r} is not configured")
    if not config.get("provider") or not config.get("model"):
        raise ExecutionError(f"profile {name!r} lacks provider/model")
    return name, config


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
    timeout_s: int = 600,
) -> dict:
    """Execute and integrate a validated plan, preserving every worktree."""
    if max_parallel < 1:
        raise ExecutionError("max_parallel must be positive")
    if timeout_s <= 0:
        raise ExecutionError("timeout_s must be positive")
    deadline = time.monotonic() + float(timeout_s)

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            cancel.set()
            raise ExecutionError(f"execution timeout after {timeout_s}s")
        return left

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
    by_id = {task_id: {"id": task_id, "status": "queued", "profile": None, "branch": None, "worktree": None, "commit": None, "checks": []} for task_id in tasks}
    child_commits: dict[str, str] = {}
    integrated = base_sha
    observed_cost = 0.0
    cost_unknown = False
    budget = project.get("budget_usd")
    try:
        budget = float(budget) if budget is not None else None
    except (TypeError, ValueError):
        raise ExecutionError("project budget_usd must be numeric") from None

    def run_one(task: dict, child_root: Path, branch: str, profile_name: str, profile_cfg: Mapping[str, Any]) -> dict:
        task_id = task["id"]
        before = _baseline(child_root)
        _emit(emit, "task.started", {"profile": profile_name, "provider": profile_cfg['provider'],
              "model": profile_cfg['model'], "branch": branch, "worktree": str(child_root)}, task_id)
        try:
            from factory.control.providers import ProviderRequest
            request = ProviderRequest(provider=str(profile_cfg["provider"]), model=str(profile_cfg["model"]), prompt=str(task.get("prompt", "")), workspace=str(child_root), timeout_s=int(timeout_s), read_only=False)
            last_assistant_text: str | None = None
            def callback(typ: Any, payload: Any = None, *extra: Any) -> None:
                # SDK adapters historically used both emit(kind, payload) and
                # emit(kind, payload, task_id); task identity is coordinator
                # owned, so ignore a provider supplied third argument.
                nonlocal last_assistant_text
                if str(typ) in {"assistant.message", "assistant_message"}:
                    if isinstance(payload, dict):
                        candidate = payload.get("text", payload.get("content"))
                    else:
                        candidate = payload
                    if candidate is not None:
                        last_assistant_text = str(candidate)
                _emit(emit, str(typ), payload if isinstance(payload, dict) else {"value": _clip(payload)}, task_id)
            result = runner.run(request, callback, cancel=cancel)
            final_text = str(getattr(result, "text", "") or "")
            if final_text and final_text != last_assistant_text:
                _emit(emit, "assistant.message", {"text": _clip(final_text)}, task_id)
            if cancel.is_set():
                return {"status": "cancelled", "error": "cancel requested"}
            changed = _status_paths(child_root, timeout_s=timeout_s)
            if any(any(part in _FORBIDDEN_PARTS for part in PurePosixPath(p).parts) or PurePosixPath(p).name in _FORBIDDEN_NAMES for p in changed):
                raise ExecutionError("worker changed forbidden metadata or secret path")
            declared = tuple(task["paths"])
            out_of_scope = tuple(p for p in changed if not _within(p, declared))
            if out_of_scope:
                raise ExecutionError(f"out-of-scope changes: {', '.join(out_of_scope)}")
            _reject_symlinks(child_root, changed)
            judge = judge_files_touched(changed)
            hooks = runner_hooks(child_root)
            if judge or hooks:
                raise ExecutionError(f"worker changed test/check infrastructure: {', '.join(judge + hooks)}")
            _guard_workspace(child_root, before, changed)
            before_hash = _working_hash(child_root, changed, timeout_s=timeout_s)
            records: list[dict] = []
            for name, argv in _check_argv(project, task.get("checks") or ()):
                record = _run_check(child_root, name, argv, timeout_s, emit, task_id, cancel)
                records.append(record)
                if record.get("cancelled"):
                    return {"status": "cancelled", "checks": records, "error": "cancel requested"}
                if record.get("timeout") or record.get("exit") != 0:
                    return {"status": "failed", "checks": records, "error": f"verification failed: {name}"}
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
            cost = getattr(result, "cost_usd", None)
            commit_hash = diff_hash(committed_diff)
            _emit(emit, "git.commit", {"commit": commit, "branch": branch, "diff_hash": commit_hash}, task_id)
            return {"status": "verified", "commit": commit, "diff_hash": commit_hash, "checks": records, "cost_usd": cost if cost is None else float(cost)}
        except EventError:
            raise
        except ExecutionError as exc:
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            return {"status": "failed", "error": f"worker error: {type(exc).__name__}: {_clip(exc)}"}

    completed: set[str] = set()
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
        futures: dict[concurrent.futures.Future, tuple[dict, Path, str, str, Mapping[str, Any]]] = {}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="factory-task")
        pending: set[concurrent.futures.Future] = set()
        try:
            for task in wave:
                remaining()
                task_id = task["id"]
                profile_name, profile_cfg = _profile(task, profiles)
                child_branch = f"factory/{run_part}-task/{task_id}"
                child_path = root / f"task-{task_id}"
                _git_ok(workspace, "worktree", "add", "-q", "-b", child_branch, str(child_path), integration_branch, timeout_s=timeout_s)
                by_id[task_id].update({"profile": profile_name, "branch": child_branch, "worktree": str(child_path), "status": "running"})
                future = pool.submit(run_one, task, child_path, child_branch, profile_name, profile_cfg)
                futures[future] = (task, child_path, child_branch, profile_name, profile_cfg)
            done, pending = concurrent.futures.wait(futures, timeout=remaining())
            if pending:
                cancel.set()
                raise ExecutionError(f"task timeout after {timeout_s}s", artifacts=artifacts)
            for future in done:
                task, child_path, child_branch, _, _ = futures[future]
                task_id = task["id"]
                result = future.result()
                by_id[task_id].update(result)
                completed.add(task_id)
                if result.get("status") == "verified":
                    _emit(emit, "task.completed", {"status": "verified", "commit": result.get("commit"), "cost_usd": result.get("cost_usd")}, task_id)
                else:
                    _emit(emit, "task.failed", {"status": result.get("status", "failed"), "error": result.get("error", "")}, task_id)
                if result.get("cost_usd") is None:
                    cost_unknown = True
                    stop_reason = "provider cost is unknown; continuation requires human review"
                    stop_kind = "unknown_cost"
                else:
                    observed_cost += float(result["cost_usd"])
                    if budget is not None and observed_cost > budget:
                        stop_reason = f"observed provider cost ${observed_cost:.4f} exceeds budget ${budget:.4f}"
                        stop_kind = "budget"
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
        # Dependents must start from the newly integrated commit.

    if cancelled:
        raise ExecutionError("execution cancelled", artifacts={**artifacts, "tasks": list(by_id.values())})
    if failed is not None:
        for task in tasks.values():
            if task["id"] not in completed:
                by_id[task["id"]]["status"] = "blocked"
        raise ExecutionError(f"task {failed} failed", artifacts={**artifacts, "tasks": list(by_id.values())})

    final_before = _baseline(integration_path)
    final_tree = _git_ok(integration_path, "rev-parse", "HEAD^{tree}", timeout_s=timeout_s)
    if _status_paths(integration_path, timeout_s=timeout_s):
        raise ExecutionError("integration worktree is dirty before final verification", artifacts={**artifacts, "tasks": list(by_id.values()), "commit": integrated})
    final_checks: list[dict] = []
    for name, argv in _check_argv(project, (project.get("checks") or {}).keys()):
        record = _run_check(integration_path, name, argv, remaining(), emit, None, cancel)
        final_checks.append(record)
        if record.get("timeout") or record.get("exit") != 0:
            raise ExecutionError(f"final verification failed: {name}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    final_changed = _status_paths(integration_path, timeout_s=timeout_s)
    if final_changed:
        raise ExecutionError(f"final verification left worktree changes: {', '.join(final_changed)}", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    if head_position(integration_path) != final_before["head"] or _git_ok(integration_path, "rev-parse", "HEAD^{tree}", timeout_s=timeout_s) != final_tree:
        raise ExecutionError("final verification changed integration HEAD or tree", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    _reject_symlinks(integration_path, final_changed)
    if judge_files_touched(final_changed) or runner_hooks(integration_path):
        raise ExecutionError("final verification changed test/check infrastructure", artifacts={**artifacts, "tasks": list(by_id.values()), "checks": final_checks, "commit": integrated})
    _guard_workspace(integration_path, final_before, final_changed)
    artifacts.update({"commit": integrated, "checks": final_checks, "tasks": list(by_id.values()), "observed_cost_usd": observed_cost})
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
