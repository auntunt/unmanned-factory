import json

from factory.harness.transcript import find_transcript, parse_tool_calls


def _write_jsonl(path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_find_transcript_by_glob_ignores_slug(tmp_path):
    """slug 是有损的（_ → -，中文整段变 -），所以只能按 session-id 全局 glob。"""
    d = tmp_path / "-private-tmp-probe-adapter"
    d.mkdir()
    target = d / "abc-123.jsonl"
    target.write_text("", encoding="utf-8")
    assert find_transcript("abc-123", root=tmp_path) == target


def test_find_transcript_missing(tmp_path):
    assert find_transcript("nope", root=tmp_path) is None


def test_parse_tool_calls_pairs_results(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "queue-operation"},
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "tooluse_1", "name": "Bash"},
            {"type": "tool_use", "id": "tooluse_2", "name": "Write"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tooluse_1",
             "is_error": False},
            {"type": "tool_result", "tool_use_id": "tooluse_2",
             "is_error": True},
        ]}},
        {"type": "last-prompt"},
    ])
    calls = parse_tool_calls(p)
    assert [c.name for c in calls] == ["Bash", "Write"]
    assert [c.is_error for c in calls] == [False, True]
    assert calls[0].call_id == "tooluse_1"


def test_parse_tool_calls_tolerates_bad_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        'not json\n'
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read"}]}})
        + "\n\n",
        encoding="utf-8",
    )
    assert [c.name for c in parse_tool_calls(p)] == ["Read"]


def test_parse_tool_calls_missing_file(tmp_path):
    assert parse_tool_calls(tmp_path / "gone.jsonl") == ()
