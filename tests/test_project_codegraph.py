from __future__ import annotations

import subprocess
import sqlite3

import pytest

from factory.control.codegraph import (
    baseline_sha,
    build_snapshot,
    get_snapshot,
    graph_slice,
    save_snapshot,
    search_snapshot,
)
from factory.control.store import Store


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    return repo, run


def _project(repo):
    return {"id": "project-1", "workspace": str(repo), "base_branch": "main"}


def test_indexes_commit_not_dirty_checkout_and_persists(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "app.py").write_text("def committed():\n    return 1\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "initial")
    original = baseline_sha(_project(repo))
    (repo / "app.py").write_text("def dirty_secret():\n    return 2\n", encoding="utf-8")
    snapshot = build_snapshot(_project(repo))
    assert snapshot["commit_sha"] == original
    assert search_snapshot(snapshot, "committed")
    assert not search_snapshot(snapshot, "dirty_secret")

    store = Store(tmp_path / "control.db")
    store.add_project({"name": "P", "repository": "owner/repo", "workspace": str(repo), "base_branch": "main"})
    snapshot["project_id"] = store.projects()[0]["id"]
    saved = save_snapshot(store, snapshot)
    assert saved["id"]
    assert get_snapshot(store, snapshot["project_id"])["commit_sha"] == original
    assert save_snapshot(store, snapshot)["id"] == saved["id"]


def test_skips_secret_symlink_and_generated_paths(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "safe.py").write_text("def safe(): pass\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=should-not-index\n", encoding="utf-8")
    (repo / "credentials.json").write_text("{}", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "bad.js").write_text("function bad() {}", encoding="utf-8")
    (repo / "link.py").symlink_to("safe.py")
    run("add", ".")
    run("commit", "-qm", "paths")
    snapshot = build_snapshot(_project(repo))
    paths = {node["path"] for node in snapshot["nodes"]}
    assert paths == {"safe.py"}
    assert all("should-not-index" not in node.get("snippet", "") for node in snapshot["nodes"])


def test_python_import_call_js_heuristic_and_graph_edges(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "main.py").write_text("import lib\ndef helper():\n    return 1\ndef run():\n    return helper()\n", encoding="utf-8")
    (repo / "ui.ts").write_text("import { thing } from './thing';\nexport function render() { return thing(); }\n", encoding="utf-8")
    (repo / "thing.ts").write_text("export function thing() { return 1; }\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "code")
    snapshot = build_snapshot(_project(repo))
    kinds = {(edge["kind"], edge["resolution"]) for edge in snapshot["edges"]}
    assert ("imports", "syntax") in kinds
    assert ("calls", "heuristic") in kinds
    assert ("imports", "heuristic") in kinds
    assert ("contains", "heuristic") in kinds
    function = next(node for node in snapshot["nodes"] if node["name"] == "run")
    sliced = graph_slice(snapshot, function["id"])
    assert function["id"] in {node["id"] for node in sliced["nodes"]}
    assert all(edge["source"] in {node["id"] for node in sliced["nodes"]} and edge["target"] in {node["id"] for node in sliced["nodes"]} for edge in sliced["edges"])


def test_limits_and_chinese_search(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "README.md").write_text("用户服务说明\n", encoding="utf-8")
    for index in range(505):
        (repo / f"f{index:03d}.py").write_text(f"def function_{index}(): pass\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "many")
    snapshot = build_snapshot(_project(repo))
    assert len(snapshot["nodes"]) <= 5000
    assert any("file_limit" in warning for warning in snapshot["warnings"])
    assert search_snapshot(snapshot, "用户")
    assert graph_slice(snapshot, limit=1)["truncated"]


def test_relative_imports_attribute_calls_and_eligible_file_budget(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
    (repo / "pkg" / "sub" / "mod.py").write_text("from ..utils import helper\nobj.helper()\n", encoding="utf-8")
    (repo / "src" / "ui").mkdir(parents=True)
    (repo / "src" / "util.ts").write_text("export function util() { return 1; }\n", encoding="utf-8")
    (repo / "src" / "ui" / "view.ts").write_text("import { util } from '../util';\nexport function view() { return util(); }\n", encoding="utf-8")
    (repo / "real.py").write_text("def real(): pass\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    for index in range(510):
        (repo / "node_modules" / f"generated{index}.py").write_text("def generated(): pass\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "resolution")
    snapshot = build_snapshot(_project(repo))
    import_edges = [e for e in snapshot["edges"] if e["kind"] == "imports"]
    assert any(e["target"] == "file:pkg/utils.py" for e in import_edges)
    assert any(e["target"] == "file:src/util.ts" for e in import_edges)
    assert not any(e["kind"] == "calls" and e["path"] == "pkg/sub/mod.py" for e in snapshot["edges"])
    assert "real.py" in {node["path"] for node in snapshot["nodes"]}


def test_snapshot_selection_is_sha_aware_and_append_only(tmp_path):
    repo, run = _repo(tmp_path)
    source = repo / "app.py"
    source.write_text("def first(): pass\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "first")
    project = _project(repo)
    first = build_snapshot(project)
    store = Store(tmp_path / "control.db")
    saved_project = store.add_project({"name": "P", "repository": "owner/sha", "workspace": str(repo), "base_branch": "main"})
    first["project_id"] = saved_project["id"]
    source.write_text("def second(): pass\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "second")
    second = build_snapshot({**project, "id": saved_project["id"]})
    save_snapshot(store, second)
    save_snapshot(store, first)
    current = get_snapshot(store, saved_project["id"])
    assert current["commit_sha"] == second["commit_sha"]
    assert get_snapshot(store, saved_project["id"], first["commit_sha"])["commit_sha"] == first["commit_sha"]
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as db:
            db.execute("UPDATE code_snapshots SET indexed_at='x'")


def test_python_call_shadowing_and_nested_resolution_are_not_syntax_facts(tmp_path):
    repo, run = _repo(tmp_path)
    (repo / "calls.py").write_text(
        "def helper(): pass\n"
        "def keyword(*, helper): helper()\n"
        "def imported(): pass\n"
        "from external import imported\n"
        "def outer():\n"
        "    def nested(): pass\n"
        "    return nested()\n"
        "obj.helper()\n",
        encoding="utf-8",
    )
    run("add", ".")
    run("commit", "-qm", "calls")
    snapshot = build_snapshot(_project(repo))
    calls = [edge for edge in snapshot["edges"] if edge["kind"] == "calls"]
    assert all(edge["resolution"] == "heuristic" for edge in calls)
    assert not any(edge["line"] == 2 for edge in calls)
    assert not any(edge["line"] == 4 for edge in calls)
    assert not any(edge["line"] == 8 for edge in calls)
    assert any(edge["line"] == 7 and edge["resolution"] == "heuristic" for edge in calls)
