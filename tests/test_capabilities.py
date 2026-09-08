import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory.control.capabilities import CapabilityStore
from factory.control.capability_routes import router
from factory.control.store import Conflict, Store


class PlannerStub:
    def __init__(self, store):
        self.store = store
        self.started = []

    def start_plan(self, run_id):
        self.started.append(run_id)


def make_app(tmp_path):
    store = Store(tmp_path / "control.db")
    project = store.add_project({
        "name": "Example", "repository": "owner/example", "workspace": str(tmp_path),
        "base_branch": "main", "checks": {}, "auto_issues": False,
        "auto_publish": False, "budget_usd": 10.0,
    })
    service = PlannerStub(store)
    app = FastAPI()

    @app.middleware("http")
    async def user(request, call_next):
        request.state.user = {"id": 1, "username": "owner", "role": "admin"}
        return await call_next(request)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return __import__("fastapi").responses.JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return __import__("fastapi").responses.JSONResponse({"detail": "missing"}, status_code=404)

    app.include_router(router(store, service))
    return app, store, project, service


def capability_body(**overrides):
    body = {
        "name": "Release checker", "description": "Checks a release.", "category": "engineering",
        "instructions": "Run the configured release checks.",
        "input_description": "Release target.", "output_description": "Evidence and result.",
        "acceptance": ["configured checks pass"], "status": "ready",
    }
    body.update(overrides)
    return body


def test_version_cas_history_and_restart_persistence(tmp_path):
    store = Store(tmp_path / "control.db")
    capabilities = CapabilityStore(store)
    first = capabilities.create(capability_body(), actor="owner")
    updated = capabilities.update(first["id"], {**capability_body(name="Release checker v2")}, 1, actor="owner")
    assert updated["revision"] == 2
    assert capabilities.get(first["id"], 1)["name"] == "Release checker"
    try:
        capabilities.update(first["id"], capability_body(), 1, actor="owner")
    except Conflict:
        pass
    else:
        raise AssertionError("stale capability revision was accepted")
    restarted = CapabilityStore(Store(tmp_path / "control.db"))
    assert restarted.get(first["id"])["name"] == "Release checker v2"
    assert len(restarted.versions(first["id"])) == 2
    assert len(restarted.list()) >= 6  # five durable draft templates plus the user capability


def test_testclient_rejects_draft_and_invokes_frozen_bound_revision(tmp_path):
    app, store, project, service = make_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/v3/capabilities", json=capability_body(status="draft"))
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        assert client.post(f"/api/v3/projects/{project['id']}/capabilities",
                           json={"capability_id": cid, "revision": 1}).status_code == 409

        ready_body = {**capability_body(status="ready"), "expected_revision": 1}
        ready = client.put(f"/api/v3/capabilities/{cid}", json=ready_body)
        assert ready.status_code == 200, ready.text
        assert client.post(f"/api/v3/projects/{project['id']}/capabilities",
                           json={"capability_id": cid, "revision": 2}).status_code == 201
        invoked = client.post(f"/api/v3/capabilities/{cid}/invoke", json={
            "project_id": project["id"], "revision": 2, "request": "Check this release now",
        })
        assert invoked.status_code == 200, invoked.text
        run = invoked.json()
        assert run["capability"]["id"] == cid
        assert run["capability"]["revision"] == 2
        assert run["source"]["type"] == "capability"
        assert service.started == [run["id"]]


def test_distill_is_deterministic_candidate_from_verified_run(tmp_path):
    app, store, project, service = make_app(tmp_path)
    run, _ = store.create_run(project["id"], "Make the release check reliable", source={"type": "test"})
    store.update(run["id"], {
        "status": "ready_for_review",
        "plan": {"summary": "Release", "tasks": [{"id": "check", "title": "Run checks",
            "acceptance": ["all checks pass"]}]},
        "artifacts": {"known": True},
    })
    with TestClient(app) as client:
        response = client.post(f"/api/v3/runs/{run['id']}/distill", json={
            "name": "Release check candidate", "category": "engineering",
        })
        assert response.status_code == 201, response.text
        candidate = response.json()
        assert candidate["status"] == "draft"
        assert candidate["source_run_id"] == run["id"]
        assert "确定性候选材料" in candidate["instructions"]
        assert "all checks pass" in candidate["acceptance"]
