import sys
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_control_app import login, project
from factory.control.app import create_app
from factory.control.service import Service
from factory.control.store import Store
from tests.test_control_app import FakeSDK


@pytest.fixture
def app_env(tmp_path):
    repo = tmp_path / "repos" / "sample"
    repo.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.com"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "greeting.txt").write_text("hello")
    subprocess.run(["git", "add", "greeting.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    data = tmp_path / "data"
    store = Store(data / "control.db")
    profiles = {role: {"provider": "codex", "model": "test"}
                for role in ("planner", "cheap", "standard", "strong")}
    service = Service(store, runner=FakeSDK(), profiles=profiles)
    app = create_app(data_dir=data, workspace_root=tmp_path / "repos",
                     public_origin="http://testserver", service=service,
                     webhook_secret="test-webhook-secret")
    app.state.auth.create_user("owner", "a-long-test-password")
    with TestClient(app) as client:
        yield client, store, service, repo


def _settings(project, *, revision=None, **changes):
    return {
        "revision": project["revision"] if revision is None else revision,
        "name": changes.get("name", project["name"]),
        "base_branch": changes.get("base_branch", project["base_branch"]),
        "checks": changes.get("checks", project["checks"]),
        "auto_issues": changes.get("auto_issues", project["auto_issues"]),
        "auto_publish": changes.get("auto_publish", project["auto_publish"]),
        "budget_usd": changes.get("budget_usd", project["budget_usd"]),
    }


def test_project_update_requires_login_and_csrf(app_env):
    client, _, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    body = _settings(created, name="Renamed")
    assert client.put(f"/api/v2/projects/{created['id']}", json=body).status_code == 403
    assert client.put(f"/api/v2/projects/{created['id']}", json=body,
                      headers={"Origin": "http://testserver"}).status_code == 403


def test_project_update_cas_busy_and_immutable_paths(app_env):
    client, store, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    changed = client.put(f"/api/v2/projects/{created['id']}", json=_settings(created, name="New name"),
                         headers=headers)
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    assert client.put(f"/api/v2/projects/{created['id']}", json=_settings(created, name="Stale"),
                      headers=headers).status_code == 409
    assert client.put(f"/api/v2/projects/{created['id']}", json={**_settings(changed.json()),
                      "workspace": str(repo)}, headers=headers).status_code == 422
    run, _ = store.create_run(created["id"], "busy request")
    store.update(run["id"], {"status": "awaiting_approval"}, expected=("received",))
    assert client.put(f"/api/v2/projects/{created['id']}", json=_settings(changed.json(), name="Busy"),
                      headers=headers).status_code == 409


@pytest.mark.parametrize("active_status", [
    "received", "planning", "queued", "running", "verifying", "publishing",
])
def test_budget_only_increase_ignores_historical_delivery_but_active_work_blocks(
        app_env, active_status):
    client, store, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    historical, _ = store.create_run(created["id"], "already verified")
    store.update(historical["id"], {"status": "ready_for_review"})
    paused, _ = store.create_run(created["id"], "resume after adding budget")
    store.update(paused["id"], {"status": "needs_human"})

    raised = client.put(f"/api/v2/projects/{created['id']}",
                        json=_settings(created, budget_usd=created["budget_usd"] + 5),
                        headers=headers)
    assert raised.status_code == 200, raised.text
    assert raised.json()["budget_usd"] == created["budget_usd"] + 5

    active, _ = store.create_run(created["id"], "currently executing")
    store.update(active["id"], {"status": active_status})
    blocked = client.put(f"/api/v2/projects/{created['id']}",
                         json=_settings(raised.json(), budget_usd=raised.json()["budget_usd"] + 5),
                         headers=headers)
    assert blocked.status_code == 409


def test_budget_decrease_and_other_edits_stay_blocked_for_unfinished_runs(app_env):
    client, store, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    paused, _ = store.create_run(created["id"], "paused for owner")
    store.update(paused["id"], {"status": "needs_human"})

    lowered = client.put(f"/api/v2/projects/{created['id']}",
                         json=_settings(created, budget_usd=created["budget_usd"] - 1),
                         headers=headers)
    assert lowered.status_code == 409
    lowered_and_renamed = client.put(f"/api/v2/projects/{created['id']}",
        json=_settings(created, budget_usd=created["budget_usd"] - 1, name="Lower and rename"),
        headers=headers)
    assert lowered_and_renamed.status_code == 409

    store.update(paused["id"], {"status": "ready_for_review"})
    renamed = client.put(f"/api/v2/projects/{created['id']}",
                         json=_settings(created, name="Renamed while delivery waits"),
                         headers=headers)
    assert renamed.status_code == 409


def test_project_readiness_reports_dirty_head_and_missing_base(app_env):
    client, store, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    (repo / "untracked.txt").write_text("dirty")
    readiness = client.get(f"/api/v2/projects/{created['id']}/readiness", headers=headers)
    assert readiness.status_code == 200
    payload = readiness.json()
    assert payload["ready"] is False
    assert any(item["id"] == "clean_head" and item["status"] == "blocked" for item in payload["checks"])

    missing = store.add_project({
        "name": "Missing base", "repository": "owner/missing-base", "workspace": str(repo),
        "base_branch": "does-not-exist", "checks": {"python": [sys.executable, "-c", "pass"]},
        "auto_issues": False, "auto_publish": False, "budget_usd": 10.0,
    })
    missing_readiness = client.get(f"/api/v2/projects/{missing['id']}/readiness", headers=headers)
    assert missing_readiness.status_code == 200
    assert any(item["id"] == "base_branch" and item["status"] == "blocked"
               for item in missing_readiness.json()["checks"])


def test_readiness_resolves_relative_check_from_project_root(app_env):
    client, _, _, repo = app_env
    headers = login(client)
    (repo / "tools").mkdir()
    check = repo / "tools" / "check.sh"
    check.write_text("#!/bin/sh\nexit 0\n")
    check.chmod(0o755)
    subprocess.run(["git", "add", "tools/check.sh"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add check"], cwd=repo, check=True)
    created = project(client, repo, headers)
    updated = client.put(f"/api/v2/projects/{created['id']}", json=_settings(
        created, checks={"relative": ["tools/check.sh"]}), headers=headers)
    assert updated.status_code == 200, updated.text
    payload = client.get(f"/api/v2/projects/{created['id']}/readiness", headers=headers).json()
    relative = next(item for item in payload["checks"] if item["id"] == "check:relative")
    assert relative["status"] == "ok"


def test_readiness_blocks_clean_head_when_base_branch_sha_differs(app_env):
    client, _, _, repo = app_env
    headers = login(client)
    created = project(client, repo, headers)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True, capture_output=True)
    (repo / "feature.txt").write_text("feature")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=repo, check=True)
    payload = client.get(f"/api/v2/projects/{created['id']}/readiness", headers=headers).json()
    assert payload["ready"] is False
    assert any(item["id"] == "head_matches_base" and item["status"] == "blocked"
               for item in payload["checks"])


def test_project_create_reports_git_start_errors_as_bad_request(app_env, monkeypatch):
    client, _, _, repo = app_env
    headers = login(client)
    import factory.control.app as app_module
    original = app_module.subprocess.run

    def fail_git(command, *args, **kwargs):
        if command and command[0] == "git":
            raise OSError("git unavailable")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(app_module.subprocess, "run", fail_git)
    response = client.post("/api/v2/projects", json={
        "name": "No Git", "repository": "owner/no-git", "workspace": str(repo),
        "base_branch": "main", "checks": {"python": [sys.executable, "-c", "pass"]},
    }, headers=headers)
    assert response.status_code == 400


def test_project_snapshot_and_run_detail_share_current_evidence(app_env):
    client, store, svc, repo = app_env
    headers = login(client)
    p = project(client, repo, headers)
    r, _ = store.create_run(p['id'], 'snapshot test')
    store.update(r['id'], {'status': 'running', 'plan': {'title': 'current'}, 'tasks': [{'id': 't', 'status': 'running', 'attempts': [{'status': 'failed', 'error': 'old'}]}]})
    svc._emit(r['id'], 'attempt.started', {'attempt': 1, 'provider': 'test'}, 't')
    summary = client.get('/api/v3/overview', params={'project_id': p['id']}).json()
    detail = client.get(f"/api/v2/runs/{r['id']}").json()
    assert summary['run_snapshots'][0]['progress'] == detail['progress']
    assert summary['run_snapshots'][0]['tasks'][0]['attempts'][-1]['status'] == 'running'
    assert len(detail['tasks'][0]['attempts']) == 2
    svc._emit(r['id'], 'check.result', {'name': 'check', 'exit': 0}, 't')
    summary = client.get('/api/v3/overview', params={'project_id': p['id']}).json()
    detail = client.get(f"/api/v2/runs/{r['id']}").json()
    assert detail['progress']['checks'] == 'passed'
    assert summary['run_snapshots'][0]['progress'] == detail['progress']
    assert next(s for s in summary['engineering']['stages'] if s['id'] == 'verify')['count'] == 1
