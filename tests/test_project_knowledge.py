"""Focused persistence and safety tests for the project knowledge store."""

from __future__ import annotations

import hashlib
import sqlite3
import threading

import pytest

from factory.control.knowledge import KnowledgeStore
from factory.control.store import Conflict, Store


@pytest.fixture
def project_store(tmp_path):
    store = Store(tmp_path / "control.db")
    project = store.add_project({
        "name": "Demo", "repository": "owner/demo", "workspace": str(tmp_path),
        "base_branch": "main", "checks": {},
    })
    return store, project


def _entry(title="Note", content="body", **extra):
    return {"title": title, "content": content, "paths": ["src/main.py"], **extra}


def test_profile_is_lazy_stable_and_restart_safe(project_store):
    store, project = project_store
    first = KnowledgeStore(store).agent(project["id"])
    assert first["revision"] == 1 and first["name"] == "Demo"
    updated = KnowledgeStore(store).update_agent(
        project["id"], {"mission": "keep it safe", "constraints": ["review"]}, 1, "alice"
    )
    assert updated["revision"] == 2 and updated["name"] == "Demo"
    reopened = KnowledgeStore(Store(store.path)).agent(project["id"])
    assert reopened["id"] == first["id"]
    assert reopened["mission"] == "keep it safe"
    assert reopened["created_at"] == first["created_at"]
    with pytest.raises(Conflict):
        KnowledgeStore(Store(store.path)).update_agent(project["id"], {"mission": "stale"}, 1, "bob")


def test_concurrent_profile_cas_allows_one_winner(project_store):
    store, project = project_store
    KnowledgeStore(store).agent(project["id"])
    barrier = threading.Barrier(2)
    outcomes = []

    def update(value):
        knowledge = KnowledgeStore(Store(store.path))
        barrier.wait(timeout=5)
        try:
            outcomes.append(knowledge.update_agent(project["id"], {"mission": value}, 1, value))
        except Conflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=update, args=("one",)), threading.Thread(target=update, args=("two",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sum(item == "conflict" for item in outcomes) == 1
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert KnowledgeStore(Store(store.path)).agent(project["id"])["revision"] == 2


def test_entry_versions_limits_cas_and_retirement(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    entry = knowledge.put_entry(project["id"], _entry(), "alice")
    with pytest.raises(Conflict):
        knowledge.put_entry(project["id"], _entry(content="stale"), "alice", entry["key"], 0)
    revised = knowledge.put_entry(project["id"], _entry(content="new"), "alice", entry["key"], 1)
    retired = knowledge.put_entry(project["id"], _entry(status="retired"), "alice", entry["key"], revised["revision"])
    assert len(knowledge.versions(project["id"], entry["key"])) == 3
    assert knowledge.entries(project["id"]) == []
    assert knowledge.entries(project["id"], include_retired=True)[0]["status"] == "retired"
    with pytest.raises(ValueError):
        knowledge.put_entry(project["id"], _entry(), "alice", entry["key"], True)


def test_secret_scrubbing_paths_and_append_only_tables(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    entry = knowledge.put_entry(project["id"], _entry(content="api_key=super-secret-value"), "alice")
    assert "super-secret-value" not in entry["content"]
    assert "REDACTED" in entry["content"]
    with pytest.raises(ValueError):
        knowledge.put_entry(project["id"], _entry(paths=["../secret.txt"]), "alice")
    with pytest.raises(ValueError):
        knowledge.put_entry(project["id"], _entry(paths=[".env.local"]), "alice")
    with store.connect() as db:
        for table in ("knowledge_entry_versions", "knowledge_audit"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"DELETE FROM {table}")


def test_import_preview_hash_tamper_and_idempotency(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    preview = knowledge.preview_import(project["id"], {
        "repository": "owner/demo",
        "documents": [{"path": "teamwiki/guide.md", "content": "# Guide\napi_key=do-not-store"}],
    }, "alice")
    assert "do-not-store" not in preview["documents"][0]["content"]
    assert preview["documents"][0]["sha256"] == hashlib.sha256(
        b"# Guide\napi_key=do-not-store"
    ).hexdigest()
    assert preview["documents"][0]["content_sha256"] == hashlib.sha256(
        preview["documents"][0]["content"].encode()
    ).hexdigest()
    with pytest.raises(Conflict):
        knowledge.apply_import(project["id"], preview["id"], "0" * 64, [0], "alice")
    first = knowledge.apply_import(project["id"], preview["id"], preview["sha256"], [0], "alice")
    second = knowledge.apply_import(project["id"], preview["id"], preview["sha256"], [0], "bob")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["entries"][0]["key"] == first["entries"][0]["key"]
    assert second["entries"][0]["provenance"]["source"] == "teamai_import"
    with pytest.raises(ValueError):
        knowledge.apply_import(project["id"], preview["id"], preview["sha256"], [], "alice")
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM knowledge_previews")


def test_merge_record_is_verified_fact_and_idempotent(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    run = {"id": "run-1", "project_id": project["id"], "plan": {"tasks": [{"paths": ["src/main.py"]}]}}
    evidence = {
        "head_sha": "a" * 40, "merge_commit_sha": "b" * 40,
        "pr_url": "https://github.com/owner/demo/pull/7", "pr_number": 7,
        "repository": "owner/demo", "base_branch": "main",
        "merged_at": "2026-09-08T00:00:00Z", "check_results": {"unit": "pass"},
    }
    first = knowledge.record_merge(project["id"], run, evidence)
    second = knowledge.record_merge(project["id"], run, evidence)
    assert first["duplicate"] is False and second["duplicate"] is True
    entry = first["entry"]
    assert entry["kind"] == "fact" and entry["status"] == "active"
    assert entry["commit_sha"] == "b" * 40 and entry["paths"] == ["src/main.py"]
    assert entry["provenance"]["run_id"] == "run-1"
    assert "plan" not in entry["content"].lower()
    assert len(knowledge.entries(project["id"])) == 1


def test_project_scoping_and_import_validation(project_store):
    store, project = project_store
    other = store.add_project({
        "name": "Other", "repository": "owner/other", "workspace": "/tmp", "base_branch": "main", "checks": {},
    })
    knowledge = KnowledgeStore(store)
    with pytest.raises(ValueError):
        knowledge.preview_import(project["id"], {"repository": "owner/other", "documents": []}, "alice")
    with pytest.raises(KeyError):
        knowledge.entries("missing")
    entry = knowledge.put_entry(other["id"], _entry(), "alice")
    assert knowledge.entries(other["id"])[0]["key"] == entry["key"]


def _fill(knowledge, pid, count):
    for index in range(count):
        knowledge.put_entry(pid, {"title": f"n{index}", "content": "x"}, "tester")


def test_entry_cap_is_enforced_for_direct_and_merge_creation(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    _fill(knowledge, project["id"], 1000)
    with pytest.raises(ValueError):
        knowledge.put_entry(project["id"], _entry(title="overflow"), "alice")
    run = {"id": "run-cap", "project_id": project["id"], "plan": {"tasks": []},
           "artifacts": {"checks": [{"name": "unit", "exit": 0}]}}
    evidence = {
        "head_sha": "a" * 40, "merge_commit_sha": "b" * 40,
        "pr_url": "https://github.com/owner/demo/pull/8", "pr_number": 8,
        "repository": "owner/demo", "base_branch": "main", "merged_at": "2026-09-08T00:00:00Z",
    }
    with pytest.raises(ValueError):
        knowledge.record_merge(project["id"], run, evidence)
    assert len(knowledge.entries(project["id"])) == 1000


def test_import_splits_long_documents_and_rolls_back_at_cap(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    long_text = "# Long\n" + ("x" * 15993)
    preview = knowledge.preview_import(project["id"], {
        "repository": "owner/demo", "documents": [{"path": "teamwiki/long.md", "content": long_text}],
    }, "alice")
    result = knowledge.apply_import(project["id"], preview["id"], preview["sha256"], [0], "alice")
    assert len(result["entries"]) == 2
    assert all(len(item["content"]) <= 8000 for item in result["entries"])
    assert [item["provenance"]["chunk_index"] for item in result["entries"]] == [0, 1]

    store2, project2 = Store(store.path + ".rollback"), None
    project2 = store2.add_project({"name": "Rollback", "repository": "owner/rollback", "workspace": "/tmp", "base_branch": "main", "checks": {}})
    knowledge2 = KnowledgeStore(store2)
    _fill(knowledge2, project2["id"], 999)
    preview2 = knowledge2.preview_import(project2["id"], {
        "repository": "owner/rollback", "documents": [
            {"path": "teamwiki/a.md", "content": "a"},
            {"path": "teamwiki/b.md", "content": "b"},
        ],
    }, "alice")
    with pytest.raises(ValueError):
        knowledge2.apply_import(project2["id"], preview2["id"], preview2["sha256"], [0, 1], "alice")
    assert len(knowledge2.entries(project2["id"])) == 999


def test_merge_bounds_paths_without_rejecting_normal_names(project_store):
    store, project = project_store
    knowledge = KnowledgeStore(store)
    run = {
        "id": "run-paths", "project_id": project["id"],
        "plan": {"tasks": [{"paths": ["tokenizer.py", *[f"src/file-{i}.py" for i in range(25)]]}]},
        "artifacts": {"checks": [{"name": "unit", "exit": 0}]},
    }
    evidence = {
        "head_sha": "a" * 40, "merge_commit_sha": "c" * 40,
        "pr_url": "https://github.com/owner/demo/pull/9", "pr_number": 9,
        "repository": "owner/demo", "base_branch": "main", "merged_at": "2026-09-08T00:00:00Z",
    }
    entry = knowledge.record_merge(project["id"], run, evidence)["entry"]
    assert len(entry["paths"]) == 20 and entry["paths"][0] == "tokenizer.py"
    assert entry["provenance"]["path_count"] == 26
    assert entry["provenance"]["omitted_path_count"] == 6
