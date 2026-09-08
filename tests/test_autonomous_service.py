from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factory.control.app import create_app
from factory.control.autonomy import PolicyStore
from factory.control.providers import ProviderResult
from factory.control.service import Service
from factory.control.store import Store


def _task(*, risk: str = "low") -> dict:
    return {"id": "greeting", "title": "Update greeting", "prompt": "write greeting",
            "acceptance": ["greeting is updated"], "paths": ["greeting.txt"],
            "checks": ["greeting"], "depends_on": [], "complexity": "small", "risk": risk}


def _plan(*, questions: list[str] | None = None, risk: str = "low") -> dict:
    return {"title": "Update greeting", "summary": "Update greeting safely",
            "questions": questions or [], "tasks": [] if questions else [_task(risk=risk)]}


class Runner:
    def __init__(self, plans: list[dict], *, planner_cost: float | None = 0.2, worker_cost: float | None = 0.3):
        self.plans = list(plans)
        self.planner_cost = planner_cost
        self.worker_cost = worker_cost
        self.planner_calls = self.worker_calls = 0
        self.models: list[tuple[bool, str]] = []

    def available(self):
        return [{"id": "codex", "installed": True, "detail": "test"}]

    def run(self, request, emit, cancel=None):
        self.models.append((request.read_only, request.model))
        if request.read_only:
            self.planner_calls += 1
            plan = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return ProviderResult(json.dumps(plan), cost_usd=self.planner_cost, tokens_in=10, tokens_out=4)
        self.worker_calls += 1
        Path(request.workspace, "greeting.txt").write_text("hello autonomous\n")
        return ProviderResult("updated", cost_usd=self.worker_cost, tokens_in=7, tokens_out=3)


def _wait(store: Store, rid: str, states: set[str]) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = store.get(rid)
        if run["status"] in states:
            return run
        time.sleep(0.02)
    raise AssertionError(store.get(rid))


@pytest.fixture
def control(tmp_path):
    repo = tmp_path / "repos" / "sample"
    repo.mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.com")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "greeting.txt").write_text("hello\n")
    subprocess.run(["git", "add", "greeting.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    store = Store(tmp_path / "data" / "control.db")
    runner = Runner([_plan()])
    profiles = {role: {"provider": "codex", "model": f"before-{role}"}
                for role in ("planner", "cheap", "standard", "strong")}
    service = Service(store, runner=runner, profiles=profiles)
    app = create_app(data_dir=tmp_path / "data", workspace_root=tmp_path / "repos",
                     public_origin="http://testserver", service=service)
    app.state.auth.create_user("owner", "a-long-test-password")
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "owner", "password": "a-long-test-password"},
                            headers={"Origin": "http://testserver"})
        assert login.status_code == 200
        headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
        project = client.post("/api/v2/projects", json={
            "name": "Sample", "repository": "owner/sample", "workspace": str(repo),
            "checks": {"greeting": [sys.executable, "-c", "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello autonomous\\n'"]},
        }, headers=headers)
        assert project.status_code == 201, project.text
        yield client, store, service, runner, project.json(), headers


def _autonomous(client, project, headers, *, risk="low", attempts=2):
    current = client.get(f"/api/v3/projects/{project['id']}/policy", headers=headers).json()
    response = client.put(f"/api/v3/projects/{project['id']}/policy", json={
        "revision": current["revision"], "mode": "autonomous", "max_risk": risk,
        "max_attempts": attempts, "auto_escalate": True, "resume_on_restart": True,
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _supervised(client, project, headers, *, risk="medium"):
    current = client.get(f"/api/v3/projects/{project['id']}/policy", headers=headers).json()
    response = client.put(f"/api/v3/projects/{project['id']}/policy", json={
        "revision": current["revision"], "mode": "supervised", "max_risk": risk,
        "max_attempts": 2, "auto_escalate": True, "resume_on_restart": True,
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _new_run(client, project, headers, request="Update greeting automatically"):
    response = client.post("/api/v2/runs", json={"project_id": project["id"], "request": request}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_projects_without_saved_policy_default_to_autonomous(control):
    client, store, service, runner, project, headers = control
    policy = client.get(f"/api/v3/projects/{project['id']}/policy", headers=headers).json()
    assert policy["revision"] == 0 and policy["mode"] == "autonomous"
    rid = _new_run(client, project, headers)
    run = _wait(store, rid, {"ready_for_review", "needs_human"})
    assert run["status"] == "ready_for_review", store.events(rid)
    assert run["policy"]["revision"] == 0 and run["policy"]["mode"] == "autonomous"
    assert "policy.authorized" in {event["type"] for event in store.events(rid)}


def test_autonomous_policy_can_adopt_waiting_plan_once(control):
    client, store, service, runner, project, headers = control
    _supervised(client, project, headers)
    rid = _new_run(client, project, headers, "Wait for explicit policy adoption")
    waiting = _wait(store, rid, {"awaiting_approval", "needs_human"})
    assert waiting["status"] == "awaiting_approval"
    current = client.get(f"/api/v3/projects/{project['id']}/policy", headers=headers).json()
    response = client.put(f"/api/v3/projects/{project['id']}/policy", json={
        "revision": current["revision"], "mode": "autonomous", "max_risk": "medium",
        "max_attempts": 2, "auto_escalate": True, "resume_on_restart": True,
        "apply_waiting": True,
    }, headers=headers)
    assert response.status_code == 200, response.text
    application = response.json()["application"]
    assert application["continued_run_ids"] == [rid] and application["blocked"] == []
    final = _wait(store, rid, {"ready_for_review", "needs_human"})
    assert final["status"] == "ready_for_review", store.events(rid)
    types = [event["type"] for event in store.events(rid)]
    assert "policy.adopted" in types and "policy.authorized" in types
    assert final["revision"] == waiting["revision"]
    assert runner.planner_calls == 1 and runner.worker_calls == 1
    again = service.apply_waiting_policy(project["id"], final["policy"], "owner")
    assert again == {"continued_run_ids": [], "blocked": []}
    versions = client.get(f"/api/v3/runs/{rid}/plans", headers=headers).json()["versions"]
    assert versions[0]["revision"] == waiting["revision"]
    assert versions[0]["plan"] == waiting["plan"]


def test_autonomous_policy_reports_waiting_risk_block_without_dispatch(control):
    client, store, service, runner, project, headers = control
    _supervised(client, project, headers)
    runner.plans = [_plan(risk="high")]
    rid = _new_run(client, project, headers, "Wait for high risk policy adoption")
    waiting = _wait(store, rid, {"awaiting_approval", "needs_human"})
    assert waiting["status"] == "awaiting_approval"
    current = client.get(f"/api/v3/projects/{project['id']}/policy", headers=headers).json()
    response = client.put(f"/api/v3/projects/{project['id']}/policy", json={
        "revision": current["revision"], "mode": "autonomous", "max_risk": "medium",
        "max_attempts": 2, "auto_escalate": True, "resume_on_restart": True,
        "apply_waiting": True,
    }, headers=headers)
    assert response.status_code == 200, response.text
    application = response.json()["application"]
    assert application["continued_run_ids"] == []
    assert application["blocked"] and application["blocked"][0]["run_id"] == rid
    assert store.get(rid)["status"] == "awaiting_approval"
    assert runner.worker_calls == 0


def test_autonomous_clear_request_runs_without_approval_and_freezes_policy_and_cost(control):
    client, store, service, runner, project, headers = control
    policy = _autonomous(client, project, headers, attempts=3)
    rid = _new_run(client, project, headers)
    run = _wait(store, rid, {"ready_for_review", "needs_human"})

    assert run["status"] == "ready_for_review", store.events(rid)
    assert runner.planner_calls == runner.worker_calls == 1
    assert run["policy"] == policy
    assert run["runtime_configuration"]["profiles"]["planner"]["model"] == "before-planner"
    assert run["planner_usage"] == {"known_cost_usd": 0.2, "unknown_cost_calls": 0, "calls": 1}
    assert run["artifacts"]["planner_cost_usd"] == pytest.approx(0.2)
    assert run["artifacts"]["total_known_cost_usd"] == pytest.approx(0.5)
    types = [event["type"] for event in store.events(rid)]
    assert "policy.authorized" in types and "human.approved" not in types
    assert run["tasks"][0]["attempts"][0]["profile"] == "cheap"


def test_autonomy_waits_for_clarification_then_continues(control):
    client, store, service, runner, project, headers = control
    runner.plans = [_plan(questions=["Which greeting should be used?"]), _plan()]
    _autonomous(client, project, headers)
    rid = _new_run(client, project, headers)
    first = _wait(store, rid, {"needs_clarification", "needs_human"})
    assert first["status"] == "needs_clarification"
    response = client.post(f"/api/v2/runs/{rid}/clarify", json={"answer": "Use hello autonomous."}, headers=headers)
    assert response.status_code == 200, response.text
    final = _wait(store, rid, {"ready_for_review", "needs_human"})
    assert final["status"] == "ready_for_review", store.events(rid)
    assert runner.planner_calls == 2 and runner.worker_calls == 1


def test_autonomy_stops_above_risk_limit_without_worker_dispatch(control):
    client, store, service, runner, project, headers = control
    runner.plans = [_plan(risk="high")]
    _autonomous(client, project, headers, risk="medium")
    rid = _new_run(client, project, headers)
    run = _wait(store, rid, {"awaiting_approval", "needs_human"})
    assert run["status"] == "awaiting_approval"
    assert run["triage"]["decision"] != "auto_execute"
    assert runner.worker_calls == 0


def test_unknown_planner_cost_is_not_zero_and_prevents_auto_worker_dispatch(control):
    client, store, service, runner, project, headers = control
    runner.planner_cost = None
    _autonomous(client, project, headers)
    rid = _new_run(client, project, headers)
    run = _wait(store, rid, {"needs_human", "ready_for_review"})
    assert run["status"] == "needs_human", store.events(rid)
    assert runner.planner_calls == 1 and runner.worker_calls == 0
    usage = [event["payload"] for event in store.events(rid) if event["type"] == "usage.recorded"]
    assert usage[0]["profile"] == "planner" and usage[0]["cost_usd"] is None
    assert run["planner_usage"]["unknown_cost_calls"] == 1


def test_v3_exports_use_all_events_and_preserve_redacted_large_archive_and_overview_counts(control):
    client, store, service, runner, project, headers = control
    run, _ = store.create_run(project["id"], "Export every recorded event")
    for number in range(2005):
        payload = {"number": number}
        if number == 2004:
            payload = {"secret": "do-not-export", "stdout": "z" * 100_001}
        store.append(run["id"], "test.event", payload)
    for number in range(201):
        store.create_run(project["id"], f"Historical autonomous run {number}")

    exported = client.get(f"/api/v3/runs/{run['id']}/export", headers=headers)
    assert exported.status_code == 200
    bundle = exported.json()
    assert bundle["event_count"] == 2006
    assert bundle["events"][-1]["payload"]["stdout"] == "z" * 100_001
    assert bundle["events"][-1]["payload"]["secret"] == "***REDACTED***"
    zipped = client.get(f"/api/v3/runs/{run['id']}/export?format=zip", headers=headers)
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert len(archive.read("events.jsonl").splitlines()) == 2006
    summary = client.get("/api/v3/overview", headers=headers).json()
    assert summary["runs"] >= 202


def test_overview_keeps_cancelled_runs_out_of_attention_but_preserves_received_demand(control):
    client, store, service, runner, project, headers = control
    run, _ = store.create_run(project["id"], "Cancelled before planning")
    store.update(run["id"], {"status": "cancelled"}, expected=("received",))

    summary = client.get("/api/v3/overview", headers=headers).json()

    assert run["id"] not in {item["id"] for item in summary["attention"]}
    intake = next(stage for stage in summary["engineering"]["stages"] if stage["id"] == "intake")
    assert run["id"] in {item["id"] for item in intake["items"]}


def test_overview_projects_are_isolated_and_paused_runs_keep_stage_evidence(control):
    client, store, service, runner, project, headers = control
    from factory.control.capabilities import CapabilityStore

    other = store.add_project({
        "name": "Other", "repository": "owner/other", "workspace": project["workspace"],
        "base_branch": "main", "checks": {}, "auto_issues": False, "auto_publish": False,
        "budget_usd": 10.0, "actor": "owner",
    })
    paused, _ = store.create_run(project["id"], "Repair the release workflow")
    store.update(paused["id"], {
        "status": "needs_human",
        "plan": {"title": "Repair release", "tasks": [{"id": "release", "checks": ["smoke"]}]},
        "tasks": [{"id": "release", "attempts": [{"status": "verified"}]}],
        "artifacts": {"tasks": [{"id": "release"}], "checks": [{"name": "smoke", "exit": 0}],
                      "commit": "abc123"},
    }, expected=("received",))
    other_run, _ = store.create_run(other["id"], "Other project's request")
    healed, _ = store.create_run(project["id"], "A previously failed request")
    store.update(healed["id"], {"status": "failed"}, expected=("received",),
                 event=("run.failed", {"message": "transient failure"}))
    store.update(healed["id"], {"status": "published"}, expected=("failed",),
                 event=("github.published", {"pr_url": "https://example.test/pr/1"}))
    foreign = CapabilityStore(store).create({
        "name": "Other project capability", "description": "foreign source", "category": "engineering",
        "instructions": "Use the other project source.", "input_description": "request",
        "output_description": "result", "acceptance": [], "status": "draft",
    }, source_run_id=other_run["id"], actor="owner")

    global_summary = client.get("/api/v3/overview", headers=headers).json()
    selected = client.get(f"/api/v3/overview?project_id={project['id']}", headers=headers)
    assert selected.status_code == 200, selected.text
    selected = selected.json()
    assert selected["project_id"] == project["id"]
    assert selected["projects"] == 1
    assert selected["runs"] == 2
    assert other_run["id"] not in {item["id"] for item in selected["attention"]}
    assert healed["id"] not in {item["id"] for item in selected["attention"]}
    stage_ids = {stage["id"] for stage in selected["engineering"]["stages"]
                 if paused["id"] in {item["id"] for item in stage["items"]}}
    assert stage_ids == {"intake", "plan", "build", "verify", "deliver"}
    hrefs = {item["href"] for stage in selected["engineering"]["stages"]
             for item in stage["items"] if item["id"] == paused["id"]}
    assert hrefs == {f"/runs/{paused['id']}?view={view}" for view in
                     ("requirements", "plan", "execution", "verification", "delivery")}
    assert all(item["project_id"] == project["id"] for item in selected["attention"])
    assert other_run["id"] not in {event["run_id"] for event in selected["recent_events"]}
    reuse = next(stage for stage in selected["engineering"]["stages"] if stage["id"] == "reuse")
    assert foreign["id"] not in {item["id"] for item in reuse["items"]}
    assert {item["id"] for item in global_summary["project_summaries"]} >= {project["id"], other["id"]}
    assert client.get("/api/v3/overview?project_id=missing", headers=headers).status_code == 404
    v2_other = client.get(f"/api/v2/runs?project_id={other['id']}", headers=headers)
    assert [item["id"] for item in v2_other.json()["runs"]] == [other_run["id"]]


def test_repeated_start_plan_has_one_durable_planner_dispatch(control):
    client, store, service, runner, project, headers = control
    _supervised(client, project, headers)

    class BlockingPlanner(Runner):
        def __init__(self):
            super().__init__([_plan()])
            self.entered, self.release = threading.Event(), threading.Event()

        def run(self, request, emit, cancel=None):
            if request.read_only:
                self.entered.set()
                assert self.release.wait(5)
            return super().run(request, emit, cancel)

    blocker = BlockingPlanner()
    service.runner = blocker
    rid = _new_run(client, project, headers)
    assert blocker.entered.wait(5)
    service.start_plan(rid)
    service.start_plan(rid)
    blocker.release.set()
    run = _wait(store, rid, {"awaiting_approval", "needs_human"})
    assert run["status"] == "awaiting_approval"
    assert blocker.planner_calls == 1


def test_recovery_resumes_received_and_preserves_writing_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.com")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "greeting.txt").write_text("hello\n")
    subprocess.run(["git", "add", "greeting.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    store = Store(tmp_path / "data" / "control.db")
    project = store.add_project({"name": "Recover", "repository": "owner/recover", "workspace": str(repo),
        "base_branch": "main", "checks": {"greeting": [sys.executable, "-c", "pass"]},
        "auto_issues": False, "auto_publish": False, "budget_usd": 10})
    profiles = {role: {"provider": "codex", "model": f"{role}-model"}
                for role in ("planner", "cheap", "standard", "strong")}
    runner = Runner([_plan()])
    received, _ = store.create_run(project["id"], "Resume durable received run")
    planning_unbilled, _ = store.create_run(project["id"], "Resume interrupted planning call")
    planning_billed, _ = store.create_run(project["id"], "Resume accounted planning call")
    queued, _ = store.create_run(project["id"], "Resume durable queued run")
    writing, _ = store.create_run(project["id"], "Preserve interrupted write")
    checkpoint = {"integration_branch": "factory/interrupted", "integration_worktree": "/preserved/worktree",
                  "base_sha": "abc", "current_commit": "def", "tasks": [], "known_cost_usd": 0.4}
    store.update(writing["id"], {"status": "running", "checkpoint": checkpoint})
    store.append(writing["id"], "execution.checkpoint", checkpoint)
    store.update(planning_unbilled["id"], {"status": "planning"})
    store.append(planning_unbilled["id"], "provider.started", {"profile": "planner", "provider": "codex", "model": "planner-model"})
    store.update(planning_billed["id"], {"status": "planning"})
    store.append(planning_billed["id"], "provider.started", {"profile": "planner", "provider": "codex", "model": "planner-model"})
    store.append(planning_billed["id"], "usage.recorded", {"profile": "planner", "provider": "codex", "model": "planner-model", "cost_usd": 0.1})
    store.update(queued["id"], {"status": "queued", "plan": _plan(), "tasks": [{**_task(), "status": "pending"}]})
    PolicyStore(store).update(project["id"], {
        "mode": "supervised", "max_risk": "medium", "max_attempts": 2,
        "auto_escalate": True, "resume_on_restart": True,
    }, 0, "test")
    service = Service(store, runner=runner, profiles=profiles)
    try:
        service.recover()
        resumed = _wait(store, received["id"], {"awaiting_approval", "needs_human"})
        resumed_unbilled = _wait(store, planning_unbilled["id"], {"needs_human"})
        resumed_billed = _wait(store, planning_billed["id"], {"awaiting_approval", "needs_human"})
        resumed_queued = _wait(store, queued["id"], {"ready_for_review", "needs_human"})
        held = _wait(store, writing["id"], {"needs_human"})
        assert resumed["status"] == "awaiting_approval"
        assert resumed_unbilled["status"] == "needs_human"
        assert resumed_billed["status"] == "awaiting_approval"
        assert resumed_queued["status"] == "ready_for_review"
        assert runner.planner_calls == 2 and runner.worker_calls == 1
        unbilled = [e["payload"] for e in store.events(planning_unbilled["id"])
                    if e["type"] == "usage.recorded" and e["payload"].get("interrupted")]
        billed = [e["payload"] for e in store.events(planning_billed["id"])
                  if e["type"] == "usage.recorded" and e["payload"].get("interrupted")]
        assert len(unbilled) == 1 and unbilled[0]["cost_usd"] is None
        assert billed == []
        assert held["recovery"]["checkpoint"] == checkpoint
        assert "重复发布" in store.events(writing["id"])[-1]["payload"]["message"]
    finally:
        service.close()


def test_same_database_allows_only_one_live_durable_coordinator(control):
    client, store, service, runner, project, headers = control
    second = Service(store, runner=Runner([_plan()]), profiles=service.runtime_settings.get()["profiles"])
    try:
        with pytest.raises(Exception, match="不能重复启动执行器"):
            second.recover()
    finally:
        second.close()


def test_existing_known_or_unknown_planner_usage_blocks_new_planner_dispatch(control):
    client, store, service, runner, project, headers = control
    known, _ = store.create_run(project["id"], "Known planning budget is exhausted")
    store.append(known["id"], "usage.recorded", {"profile": "planner", "cost_usd": project["budget_usd"]})
    service.start_plan(known["id"])
    known_run = _wait(store, known["id"], {"needs_human"})
    assert runner.planner_calls == 0
    assert not any(e["type"] == "provider.started" for e in store.events(known["id"]))

    unknown, _ = store.create_run(project["id"], "Clarify must not dispatch after unknown charge")
    store.update(unknown["id"], {"status": "needs_clarification"})
    store.append(unknown["id"], "usage.recorded", {"profile": "planner", "cost_usd": None})
    service.clarify(unknown["id"], "Use a precise greeting", "owner")
    unknown_run = _wait(store, unknown["id"], {"needs_human"})
    assert unknown_run["status"] == known_run["status"] == "needs_human"
    assert runner.planner_calls == 0
    assert not any(e["type"] == "provider.started" for e in store.events(unknown["id"]))


def test_retry_double_click_creates_one_linked_successor(control):
    client, store, service, runner, project, headers = control
    prior, _ = store.create_run(project["id"], "Retry this failed run")
    store.update(prior["id"], {"status": "needs_human"}, event=("run.failed", {"message": "verification failed"}))
    first = service.retry(prior["id"], "owner")
    second = service.retry(prior["id"], "owner")
    assert first["id"] == second["id"]
    assert len(store.all_runs()) == 2
    successor = _wait(store, first["id"], {"ready_for_review", "needs_human"})
    assert successor["status"] == "ready_for_review"
    assert runner.worker_calls == 1
    assert successor["previous_run_id"] == prior["id"]
    assert len([event for event in store.events(prior["id"]) if event["type"] == "run.retry_linked"]) == 1


def test_waiting_adoption_rechecks_source_and_retained_questions(control):
    client, store, service, runner, project, headers = control
    run = {"id": "not-dispatched", "source": {"type": "github", "trusted_label": False},
           "plan": _plan(), "triage": {"decision": "auto_execute", "questions": []},
           "request": "Update greeting"}
    policy = service.policies.get(project["id"])
    reason, decision = service._waiting_policy_check(run, project, policy)
    assert reason and decision["decision"] != "auto_execute"
    run["source"] = {"type": "web"}
    run["triage"]["questions"] = ["Which greeting?"]
    reason, decision = service._waiting_policy_check(run, project, policy)
    assert reason and decision["questions"] == ["Which greeting?"]
    assert runner.planner_calls == runner.worker_calls == 0
