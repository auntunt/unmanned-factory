from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory.control.app import create_app
from factory.control.execution import ExecutionError, execute_plan
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store


ROLES = ("planner", "cheap", "standard", "strong")


@dataclass
class Result:
    text: str = "done"
    cost_usd: float | None = 0.10


class ExecutionRunner:
    """A local worker double which edits only the requested file."""

    def __init__(self, costs: dict[str, float | None]):
        self.costs = costs
        self.seen: list[tuple[str, str, str]] = []

    def run(self, request, emit, cancel=None):
        name = request.prompt.split()[-1]
        self.seen.append((name, request.model, request.workspace))
        Path(request.workspace, f"{name}.txt").write_text(f"{name}\n")
        return Result(cost_usd=self.costs.get(name, 0.10))


class ServiceRunner:
    """Fake planner/worker SDK for service lifecycle tests."""

    def __init__(self, *, plans: list[dict], worker_cost: float | None = 0.10):
        self.plans = list(plans)
        self.worker_cost = worker_cost
        self.calls: list[tuple[bool, str, str]] = []

    def run(self, request, emit, cancel=None):
        self.calls.append((request.read_only, request.model, request.prompt))
        if request.read_only:
            plan = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return ProviderResult(json.dumps(plan), cost_usd=0.01)
        # The service plan used by these tests names its concrete file in the
        # prompt. This keeps the worker deterministic without a model call.
        if "first" in request.prompt:
            name = "first"
        elif "second" in request.prompt:
            name = "second"
        else:
            name = "greeting"
        Path(request.workspace, f"{name}.txt").write_text(f"{name}\n")
        return ProviderResult("changed", cost_usd=self.worker_cost)


class Publisher:
    def __init__(self):
        self.calls = 0

    def publish(self, project, run):
        self.calls += 1
        return {"pr_url": "https://github.example/pr/1"}


def _profiles(prefix: str = "old"):
    return {role: {"provider": "codex", "model": f"{prefix}-{role}"} for role in ROLES}


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.com")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _checks() -> dict[str, list[str]]:
    return {
        name: [sys.executable, "-c", f"from pathlib import Path; assert Path('{name}.txt').read_text() == '{name}\\n'"]
        for name in ("first", "second", "greeting")
    }


def _task(name: str, *, depends_on: list[str] | None = None,
          complexity: str = "small", risk: str = "low") -> dict:
    return {
        "id": name,
        "title": name,
        "prompt": f"write {name}",
        "acceptance": [f"{name} is written"],
        "paths": [f"{name}.txt"],
        "checks": [name],
        "depends_on": depends_on or [],
        "complexity": complexity,
        "risk": risk,
    }


def _plan(*tasks: dict) -> dict:
    return {"title": "test", "summary": "test", "questions": [], "tasks": list(tasks)}


def _project(repo: Path, *, auto_publish: bool = False) -> dict:
    return {
        "workspace": str(repo),
        "base_branch": "main",
        # The task checks prove the worker's scope; this independent final
        # check keeps the integration test focused on billing state/content.
        "checks": {name: [sys.executable, "-c", "pass"] for name in ("first", "second")},
        "budget_usd": 10.0,
        "auto_publish": auto_publish,
    }


def test_unknown_cost_stop_blocks_second_wave_and_preserves_known_subtotal(tmp_path):
    repo = _git_repo(tmp_path)
    runner = ExecutionRunner({"first": None, "second": 0.25})
    with pytest.raises(ExecutionError) as caught:
        execute_plan(
            run_id="unknown-stop",
            plan={"tasks": [_task("first"), _task("second", depends_on=["first"])]},
            project=_project(repo),
            profiles=_profiles(),
            runner=runner,
            emit=lambda *_: None,
            cancel=threading.Event(),
        )

    artifacts = caught.value.artifacts
    assert artifacts is not None
    assert [name for name, _, _ in runner.seen] == ["first"]
    assert artifacts["observed_cost_usd"] is None
    assert artifacts["known_cost_usd"] == 0
    assert artifacts["billing_incomplete"]
    assert artifacts["autopublish_blocked"] is True


def test_unknown_cost_allow_bounded_runs_second_wave_and_records_final_content(tmp_path):
    repo = _git_repo(tmp_path)
    runner = ExecutionRunner({"first": None, "second": 0.25})
    artifacts = execute_plan(
        run_id="unknown-allow",
        plan={"tasks": [_task("first"), _task("second", depends_on=["first"])]},
        project={**_project(repo), "unknown_cost_policy": "allow_bounded"},
        profiles=_profiles(),
        runner=runner,
        emit=lambda *_: None,
        cancel=threading.Event(),
    )

    assert [name for name, _, _ in runner.seen] == ["first", "second"]
    assert artifacts["observed_cost_usd"] is None
    assert artifacts["known_cost_usd"] == pytest.approx(0.25)
    assert artifacts["billing_incomplete"]
    assert artifacts["autopublish_blocked"] is True
    integration = Path(artifacts["worktree"])
    assert (integration / "first.txt").read_text() == "first\n"
    assert (integration / "second.txt").read_text() == "second\n"


def _app_env(tmp_path: Path, runner: ServiceRunner, *, profiles=None, publisher=None):
    repo = tmp_path / "repos" / "sample"
    repo.mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.com")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "greeting.txt").write_text("base\n")
    subprocess.run(["git", "add", "greeting.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    data = tmp_path / "data"
    store = Store(data / "control.db")
    service = Service(store, runner=runner, profiles=profiles or _profiles(), publisher=publisher)
    app = create_app(data_dir=data, workspace_root=tmp_path / "repos",
                     public_origin="http://testserver", service=service)
    app.state.auth.create_user("owner", "a-long-test-password")
    return app, store, service, repo


def _wait(store: Store, rid: str, states: set[str]):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.get(rid)
        if run["status"] in states:
            return run
        time.sleep(0.02)
    raise AssertionError(store.get(rid))


def _login(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "owner", "password": "a-long-test-password"},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 200, response.text
    return {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf_token"]}


def _create_project(client: TestClient, repo: Path, headers, *, auto_publish=False):
    response = client.post("/api/v2/projects", json={
        "name": "Sample", "repository": "owner/sample", "workspace": str(repo),
        "auto_publish": auto_publish, "checks": {"greeting": _checks()["greeting"]},
    }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _update_runtime(service: Service, *, prefix: str, max_tasks=20, unknown_cost_policy="stop"):
    current = service.runtime_settings.get()
    return service.runtime_settings.update({
        "profiles": _profiles(prefix),
        "limits": {**current["limits"], "max_tasks": max_tasks,
                    "unknown_cost_policy": unknown_cost_policy},
    }, current["revision"], "owner")


def test_planned_run_uses_frozen_profiles_after_runtime_update(tmp_path):
    plans = [_plan(_task("greeting", complexity="large")), _plan(_task("greeting", complexity="large"))]
    runner = ServiceRunner(plans=plans)
    app, store, service, repo = _app_env(tmp_path, runner)
    with TestClient(app) as client:
        headers = _login(client)
        project = _create_project(client, repo, headers)
        from factory.control.autonomy import DEFAULT_POLICY
        service.policies.update(project["id"], {**DEFAULT_POLICY, "mode": "supervised"}, 0, "test")
        first_id = client.post("/api/v2/runs", json={"project_id": project["id"], "request": "first"}, headers=headers).json()["id"]
        planned = _wait(store, first_id, {"awaiting_approval"})
        old_config = planned["runtime_configuration"]
        _update_runtime(service, prefix="new")
        assert client.post(f"/api/v2/runs/{first_id}/approve", json={"revision": planned["revision"]}, headers=headers).status_code == 200
        _wait(store, first_id, {"ready_for_review"})

        planner_models = [model for read_only, model, _ in runner.calls if read_only]
        worker_models = [model for read_only, model, _ in runner.calls if not read_only]
        assert planner_models[0] == old_config["profiles"]["planner"]["model"]
        assert worker_models[0] == old_config["profiles"]["strong"]["model"]

        second_id = client.post("/api/v2/runs", json={"project_id": project["id"], "request": "second"}, headers=headers).json()["id"]
        second = _wait(store, second_id, {"awaiting_approval"})
        assert second["runtime_configuration"]["revision"] > old_config["revision"]
        assert runner.calls[-1][1] == "new-planner"
        assert client.post(f"/api/v2/runs/{second_id}/approve", json={"revision": second["revision"]}, headers=headers).status_code == 200
        _wait(store, second_id, {"ready_for_review"})
        assert runner.calls[-1][1] == "new-strong"


def test_unknown_cost_never_auto_publishes(tmp_path):
    runner = ServiceRunner(plans=[_plan(_task("greeting"))], worker_cost=None)
    publisher = Publisher()
    app, store, service, repo = _app_env(tmp_path, runner, publisher=publisher)
    with TestClient(app) as client:
        headers = _login(client)
        project = _create_project(client, repo, headers, auto_publish=True)
        from factory.control.autonomy import DEFAULT_POLICY
        service.policies.update(project["id"], {**DEFAULT_POLICY, "mode": "supervised"}, 0, "test")
        rid = client.post("/api/v2/runs", json={"project_id": project["id"], "request": "publish"}, headers=headers).json()["id"]
        planned = _wait(store, rid, {"awaiting_approval"})
        assert client.post(f"/api/v2/runs/{rid}/approve", json={"revision": planned["revision"]}, headers=headers).status_code == 200
        run = _wait(store, rid, {"ready_for_review"})
        assert publisher.calls == 0
        assert run["artifacts"]["billing_incomplete"]
        assert run["artifacts"]["autopublish_blocked"] is True


def test_task_count_limit_is_checked_before_execution(tmp_path):
    repo = _git_repo(tmp_path)
    runner = ExecutionRunner({"first": 0.1, "second": 0.1})
    with pytest.raises(ExecutionError, match="task limit"):
        execute_plan(
            run_id="limit",
            plan={"tasks": [_task("first"), _task("second")]},
            project={**_project(repo), "max_tasks": 1},
            profiles=_profiles(),
            runner=runner,
            emit=lambda *_: None,
            cancel=threading.Event(),
        )
    assert runner.seen == []


def test_missing_planner_model_enters_needs_human_with_visible_error(tmp_path):
    runner = ServiceRunner(plans=[_plan(_task("greeting"))])
    profiles = _profiles()
    profiles["planner"]["model"] = ""
    app, store, service, repo = _app_env(tmp_path, runner, profiles=profiles)
    with TestClient(app) as client:
        headers = _login(client)
        project = _create_project(client, repo, headers)
        rid = client.post("/api/v2/runs", json={"project_id": project["id"], "request": "missing model"}, headers=headers).json()["id"]
        run = _wait(store, rid, {"needs_human"})
        assert "model" in store.events(rid)[-1]["payload"]["message"] or "模型" in store.events(rid)[-1]["payload"]["message"]
        assert not runner.calls
        assert run["status"] == "needs_human"
