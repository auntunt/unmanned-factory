from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from factory.control.providers import ProviderResult
from factory.control.runtime import RuntimeSettings
from factory.control.runtime_routes import router
from factory.control.store import Store


class FakeRunner:
    def __init__(self):
        self.requests = []

    def run(self, request, emit, cancel):
        self.requests.append(request)
        return ProviderResult("OK")


class FakeService:
    def __init__(self, settings):
        self.runtime_settings = settings
        self.runner = FakeRunner()


def app_for(tmp_path):
    service = FakeService(RuntimeSettings(Store(tmp_path / "control.db"), profiles={
        role: {"provider": "codex", "model": "test-model"}
        for role in ("planner", "cheap", "standard", "strong")
    }))
    app = FastAPI()

    @app.middleware("http")
    async def user(request: Request, call_next):
        request.state.user = {"username": "owner"}
        return await call_next(request)

    app.include_router(router(Store(tmp_path / "control.db"), service,
                               workspace_root=tmp_path / "repos", static_dir=tmp_path / "dist"))
    return app, service


def test_runtime_get_update_and_stale_revision(tmp_path):
    app, _ = app_for(tmp_path)
    with TestClient(app) as client:
        current = client.get("/api/v2/runtime")
        assert current.status_code == 200
        payload = current.json()
        assert payload["configuration_revision"] == 1
        response = client.put("/api/v2/runtime/profiles", json={
            "revision": 1,
            "profiles": {role: {"provider": "claude", "model": "m"}
                         for role in ("planner", "cheap", "standard", "strong")},
            "limits": payload["limits"],
        })
        assert response.status_code == 200
        stale = client.put("/api/v2/runtime/profiles", json={
            "revision": 1, "profiles": payload["profiles"], "limits": payload["limits"]})
        assert stale.status_code == 409


def test_probe_is_explicit_and_dsh_is_unsupported(tmp_path):
    app, service = app_for(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v2/runtime/probe", json={"profile": "cheap", "configuration_revision": 1})
        assert response.status_code == 200
        assert response.json()["outcome"] == "passed"
        assert len(service.runner.requests) == 1
        request = service.runner.requests[0]
        assert request.read_only is True and request.timeout_s == 30
        assert "exactly" in request.prompt


def test_probe_wrong_response_and_exception_never_leak(tmp_path):
    app, service = app_for(tmp_path)
    def wrong(*args):
        return ProviderResult('secret-provider-payload')
    service.runner.run = wrong
    with TestClient(app) as client:
        response = client.post('/api/v2/runtime/probe', json={'profile': 'cheap', 'configuration_revision': 1})
        assert response.json()['outcome'] == 'failed'
        assert 'secret-provider-payload' not in response.text
    assert 'secret-provider-payload' not in str(service.runtime_settings.last_probes())
