import io
import threading
import zipfile

import pytest

from factory.control.agents import AgentStore, inspect_skill
from factory.control.store import Conflict, Store


def make_store(tmp_path):
    return AgentStore(Store(tmp_path / "agents.db"))


def test_apply_is_idempotent_and_rollback_allocates_fresh_version(tmp_path):
    agents = make_store(tmp_path)
    agent = agents.create({"name": "维护员"}, "tester")
    aid = agent["id"]
    agents.save_draft(aid, {"instructions": "first"}, 0)
    applied = agents.apply(aid, 1, "request-1")
    assert agents.apply(aid, 0, "request-1")["id"] == applied["id"]
    agents.save_draft(aid, {"instructions": "different"}, 0)
    with pytest.raises(Conflict, match="幂等键"):
        agents.apply(aid, 1, "request-1")
    rolled = agents.rollback(aid, 1)["version"]
    assert rolled["version"] == 3
    assert [v["version"] for v in agents.versions(aid)] == [3, 2, 1]


def test_draft_cas_and_unsafe_changes_are_rejected(tmp_path):
    agents = make_store(tmp_path)
    aid = agents.create({"name": "维护员", "acceptance": ["必须保留"]}, "tester")["id"]
    agents.save_draft(aid, {"instructions": "a"}, 0)
    with pytest.raises(Conflict):
        agents.save_draft(aid, {"instructions": "b"}, 0)
    with pytest.raises(ValueError, match="工具范围"):
        agents.save_draft(aid, {"tool_scope": ["shell"]}, 1, allow_scope_change=True)
    with pytest.raises(ValueError, match="放松"):
        agents.save_draft(aid, {"acceptance": []}, 1, allow_acceptance_relax=False)


def test_two_threads_same_draft_revision_only_one_wins(tmp_path):
    agents = make_store(tmp_path)
    aid = agents.create({"name": "维护员"}, "tester")["id"]
    barrier = threading.Barrier(2)
    results = []

    def save(text):
        barrier.wait()
        try:
            agents.save_draft(aid, {"instructions": text}, 0)
            results.append("ok")
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=save, args=(str(i),)) for i in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(results) == ["conflict", "ok"]


def test_conversation_append_has_compare_and_set(tmp_path):
    agents = make_store(tmp_path)
    aid = agents.create({"name": "维护员"}, "tester")["id"]
    conversation = agents.create_conversation(aid, "maintain")
    current = agents.conversation(conversation["id"])
    updated = agents.append_message(conversation["id"], "user", "one", expected_updated_at=current["updated_at"])
    with pytest.raises(Conflict):
        agents.append_message(conversation["id"], "user", "stale", expected_updated_at=current["updated_at"])
    assert len(updated["messages"]) == 1


def test_skill_rejects_traversal_case_collision_and_missing_entry(tmp_path):
    def archive(items):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive_file:
            for name, body in items:
                archive_file.writestr(name, body)
        return out.getvalue()

    with pytest.raises(ValueError):
        inspect_skill(archive([("../escape.txt", b"x")]))
    with pytest.raises(ValueError, match="重复"):
        inspect_skill(archive([("Readme.md", b"a"), ("README.md", b"b")]))
    with pytest.raises(ValueError, match="入口"):
        inspect_skill(archive([("SKILL.md", b"---\nentry: run.py\n---\n")]))
    with pytest.raises(ValueError):
        inspect_skill(archive([("bad\x01.txt", b"x")]))


def test_nonempty_model_parameters_are_rejected(tmp_path):
    agents = make_store(tmp_path)
    with pytest.raises(ValueError, match="参数"):
        agents.create({"name": "维护员", "model_settings": {"default": {"provider": "codex", "model": "x", "parameters": {"temperature": 0}}}}, "tester")
