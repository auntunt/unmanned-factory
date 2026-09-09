from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.control.execution import ExecutionError, execute_plan
from factory.control.model_routing import RoutingError, select_profile
from factory.control.auth import AuthStore
from factory.control.governance import Governance, GovernedRunner
from factory.control.store import Store


@dataclass
class Result:
    text: str = "changed"
    cost_usd: float | None = 0.0
    session_id: str | None = "test-session"
    tokens_in: int | None = 11
    tokens_out: int | None = 7


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _task() -> dict:
    return {"id": "repair", "prompt": "write repair", "acceptance": ["repair"],
            "paths": ["repair.txt"], "checks": ["verify"], "complexity": "small", "risk": "low"}


def _profiles() -> dict:
    return {role: {"provider": "fake", "model": f"{role}-model"}
            for role in ("cheap", "standard", "strong")}


class RepairRunner:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def run(self, request, emit, cancel=None):
        self.calls.append((request.model, request.prompt))
        if len(self.calls) == 1:
            self.workspace = request.workspace
        else:
            assert request.workspace == self.workspace
            assert request.session_id == 'test-session'
            assert Path(request.workspace, 'repair.txt').read_text() == 'bad\n'
            assert 'AssertionError' in request.prompt
        body = "bad\n" if len(self.calls) == 1 else "good\n"
        Path(request.workspace, "repair.txt").write_text(body)
        return Result(cost_usd=0.15 if len(self.calls) == 1 else 0.25)


def _project(repo: Path, *, routing_policy=None) -> dict:
    project = {"workspace": str(repo), "base_branch": "main", "budget_usd": 5,
               "checks": {"verify": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').read_text() == 'good\\n'"]}}
    if routing_policy is not None:
        project["routing_policy"] = routing_policy
    return project


def test_select_profile_uses_economical_then_configured_escalation():
    profiles = _profiles()
    first = select_profile(_task(), profiles)
    second = select_profile(_task(), profiles, attempt=2, auto_escalate=True)
    third = select_profile(_task(), profiles, attempt=3, auto_escalate=True)
    assert first == {"profile": "cheap", "provider": "fake", "model": "cheap-model",
                     "reason": "small low-risk bounded task uses the economical configured profile", "attempt": 1}
    assert second["profile"] == "standard"
    assert second["attempt"] == 2
    assert "escalating" in second["reason"]
    assert third["profile"] == "strong"


def test_select_profile_exposes_missing_required_profile():
    with pytest.raises(RoutingError, match="cheap.*not configured"):
        select_profile(_task(), {"standard": {"provider": "fake", "model": "m"}})


def test_retry_preserves_workspace_session_and_repairs_before_escalating(tmp_path):
    repo = _repo(tmp_path)
    runner = RepairRunner()
    events: list[tuple[str, dict, str | None]] = []
    artifacts = execute_plan(run_id="repair-run", plan={"tasks": [_task()]}, project=_project(
        repo, routing_policy={"max_attempts": 2, "auto_escalate": True}), profiles=_profiles(), runner=runner,
        emit=lambda kind, payload, task_id=None: events.append((kind, payload, task_id)), cancel=threading.Event())

    assert [model for model, _ in runner.calls] == ["cheap-model", "cheap-model"]
    assert "Previous attempt failed" in runner.calls[1][1]
    task = artifacts["tasks"][0]
    assert task["known_cost_usd"] == pytest.approx(0.4)
    assert task["cost_usd"] == pytest.approx(0.4)
    assert artifacts["known_cost_usd"] == pytest.approx(0.4)
    assert [event[1]["attempt"] for event in events if event[0] == "usage.recorded"] == [1, 2]
    assert [event[1]["profile"] for event in events if event[0] == "model.selected"] == ["cheap", "cheap"]
    assert [event[0] for event in events].count("task.failed") == 0
    assert [event[0] for event in events].count("execution.checkpoint") >= 2


def test_scope_violation_does_not_retry_or_escalate(tmp_path):
    repo = _repo(tmp_path)

    class ScopeRunner:
        calls = 0

        def run(self, request, emit, cancel=None):
            self.calls += 1
            Path(request.workspace, "outside.txt").write_text("bad\n")
            return Result(cost_usd=0.2)

    runner = ScopeRunner()
    with pytest.raises(ExecutionError, match="failed") as caught:
        execute_plan(run_id="scope-run", plan={"tasks": [_task()]}, project=_project(
            repo, routing_policy={"max_attempts": 3, "auto_escalate": True}), profiles=_profiles(), runner=runner,
            emit=lambda *_: None, cancel=threading.Event())
    assert runner.calls == 1
    assert caught.value.artifacts["tasks"][0]["attempts"][0]["cost_usd"] == pytest.approx(0.2)


def test_unknown_cost_stop_prevents_a_retry_before_another_provider_call(tmp_path):
    repo = _repo(tmp_path)

    class UnknownRunner:
        calls = 0

        def run(self, request, emit, cancel=None):
            self.calls += 1
            Path(request.workspace, "repair.txt").write_text("bad\n")
            return Result(cost_usd=None)

    runner = UnknownRunner()
    with pytest.raises(ExecutionError, match="failed") as caught:
        execute_plan(run_id="unknown-retry", plan={"tasks": [_task()]}, project=_project(
            repo, routing_policy={"max_attempts": 3, "auto_escalate": True}), profiles=_profiles(), runner=runner,
            emit=lambda *_: None, cancel=threading.Event())
    assert runner.calls == 1
    assert caught.value.artifacts["tasks"][0]["attempts"][0]["cost_usd"] is None


def test_quota_denial_before_execution_emits_no_provider_or_usage_evidence(tmp_path):
    repo = _repo(tmp_path)
    auth = AuthStore(tmp_path / "users.db")
    member = auth.create_user("member", "sufficiently-long-password", role="member")
    store = Store(tmp_path / "control.db")
    stored_project = store.add_project({"name": "Example", "repository": "owner/example"})
    run, _ = store.create_run(stored_project["id"], "Repair it", source={"actor_id": member["id"]})
    governance = Governance(auth, store)
    governance.assign(member["id"], [stored_project["id"]], "owner")
    governance.set_limit("member", member["id"], 0, "owner")
    events: list[tuple[str, dict, str | None]] = []

    class Runner:
        entered = False

        def run(self, request, emit, cancel=None):
            self.entered = True
            return Result()

    raw_runner = Runner()
    with pytest.raises(ExecutionError, match="task repair failed"):
        execute_plan(run_id=run["id"], plan={"tasks": [_task()]}, project=_project(repo),
                     profiles=_profiles(), runner=GovernedRunner(raw_runner, governance, run["id"]),
                     emit=lambda kind, payload, task_id=None: events.append((kind, payload, task_id)),
                     cancel=threading.Event())
    assert not raw_runner.entered
    assert not [event for event in events if event[0] in {"quota.reserved", "provider.started", "usage.recorded"}]


def test_failed_provider_attempt_keeps_streamed_cost_and_cached_token_evidence(tmp_path):
    repo = _repo(tmp_path)
    events: list[tuple[str, dict, str | None]] = []

    class StreamFailure:
        def run(self, request, emit, cancel=None):
            emit("provider.usage", {"total": {"input_tokens": 31, "output_tokens": 7,
                                       "cached_input_tokens": 19, "cost_usd": 0.12}})
            raise RuntimeError("provider ended after metering")

    with pytest.raises(ExecutionError, match="failed"):
        execute_plan(run_id="streamed-usage", plan={"tasks": [_task()]}, project=_project(repo),
                     profiles=_profiles(), runner=StreamFailure(),
                     emit=lambda kind, payload, task_id=None: events.append((kind, payload, task_id)),
                     cancel=threading.Event())
    usage = next(payload for kind, payload, _ in events if kind == "usage.recorded")
    assert usage["cost_usd"] == pytest.approx(0.12)
    assert usage["input_tokens"] == 31
    assert usage["cached_input_tokens"] == 19


def test_final_integration_check_gets_one_bounded_repair_in_a_real_worktree(tmp_path):
    repo = _repo(tmp_path)
    task = {**_task(), "checks": ["task-check"]}
    project = _project(repo, routing_policy={"max_attempts": 2, "auto_escalate": True})
    project["checks"] = {
        "task-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').exists()"],
        "integration-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').read_text() == 'integrated\\n'"],
    }
    calls = []

    class IntegrationRunner:
        def run(self, request, emit, cancel=None):
            calls.append(request.prompt)
            Path(request.workspace, "repair.txt").write_text("integrated\n" if "integrated delivery failed" in request.prompt else "task\n")
            return Result(cost_usd=0.1)

    artifacts = execute_plan(run_id="integration-repair", plan={"tasks": [task]}, project=project,
        profiles=_profiles(), runner=IntegrationRunner(), emit=lambda *_: None, cancel=threading.Event())
    assert len(calls) == 2
    repair = next(item for item in artifacts["tasks"] if item["id"] == "integration-repair")
    assert repair["status"] == "verified"
    assert artifacts["commit"]


def test_integration_repair_has_one_model_attempt_even_when_task_retries_allow_three(tmp_path):
    repo = _repo(tmp_path)
    task = {**_task(), "checks": ["task-check"]}
    project = _project(repo, routing_policy={"max_attempts": 3, "auto_escalate": True})
    project["checks"] = {
        "task-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').read_text() == 'task\\n'"],
        "integration-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').read_text() == 'integrated\\n'"],
    }
    calls = []

    class FailingRepairRunner:
        def run(self, request, emit, cancel=None):
            calls.append(request.prompt)
            Path(request.workspace, "repair.txt").write_text("bad\n" if "integrated delivery failed" in request.prompt else "task\n")
            return Result(cost_usd=0.1)

    with pytest.raises(ExecutionError, match="integration repair failed"):
        execute_plan(run_id="one-repair-attempt", plan={"tasks": [task]}, project=project,
                     profiles=_profiles(), runner=FailingRepairRunner(), emit=lambda *_: None,
                     cancel=threading.Event())
    assert len(calls) == 2


def test_integration_repair_does_not_dispatch_after_known_budget_is_exhausted(tmp_path):
    repo = _repo(tmp_path)
    task = {**_task(), "checks": ["task-check"]}
    project = _project(repo, routing_policy={"max_attempts": 2, "auto_escalate": True})
    project["budget_usd"] = 0.1
    project["checks"] = {
        "task-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').exists()"],
        "integration-check": [sys.executable, "-c", "from pathlib import Path; assert Path('repair.txt').read_text() == 'integrated\\n'"],
    }
    calls = []

    class BudgetRunner:
        def run(self, request, emit, cancel=None):
            calls.append(request.prompt)
            Path(request.workspace, "repair.txt").write_text("task\n")
            return Result(cost_usd=0.1)

    with pytest.raises(ExecutionError, match="repair not dispatched.*exhausted budget"):
        execute_plan(run_id="budgeted-repair", plan={"tasks": [task]}, project=project,
                     profiles=_profiles(), runner=BudgetRunner(), emit=lambda *_: None,
                     cancel=threading.Event())
    assert len(calls) == 1


def test_missing_check_executable_does_not_dispatch_model(tmp_path):
    repo = _repo(tmp_path)
    project = _project(repo, routing_policy={'max_attempts': 3, 'auto_escalate': True})
    project['checks'] = {'verify': ['/definitely-missing-webuddy-test-runner']}
    runner = RepairRunner()
    with pytest.raises(ExecutionError) as caught:
        execute_plan(run_id='missing-check', plan={'tasks': [_task()]}, project=project,
                     profiles=_profiles(), runner=runner, emit=lambda *a: None, cancel=threading.Event())
    assert not runner.calls
    assert caught.value.artifacts['tasks'][0]['failure_kind'] == 'check_configuration'


def test_check_environment_failure_is_distinct_from_assertion_failure():
    from factory.control.execution import _check_failure_kind
    assert _check_failure_kind({'exit': 1, 'stderr': "ModuleNotFoundError: No module named 'pytest'"}) == 'check_environment'
    assert _check_failure_kind({'exit': 127, 'stderr': 'pytest: command not found'}) == 'check_configuration'
    assert _check_failure_kind({'exit': 1, 'stderr': 'AssertionError: expected 42'}) == 'verification'
