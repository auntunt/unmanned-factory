import json
import os
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

from factory.control.providers import (
    ProviderCancelled,
    ProviderRequest,
    ProviderResult,
    ProviderTimeout,
    ProviderError,
    SDKRunner,
)


def _worker(tmp_path: Path, body: str) -> tuple[str, Path]:
    script = tmp_path / "fake_sdk_worker.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return sys.executable, script


def test_parent_reads_jsonl_and_keeps_request_out_of_shell(tmp_path):
    python, script = _worker(
        tmp_path,
        """
        import json, os, sys
        request = json.loads(sys.stdin.readline())
        assert request["prompt"] == "$(touch SHOULD_NOT_EXIST); 'quoted'"
        assert "FACTORY_GITHUB_TOKEN" not in os.environ
        print(json.dumps({"type":"provider.session","payload":{"session_id":"s1"}}), flush=True)
        print(json.dumps({"type":"assistant.message","payload":{"text":"done"}}), flush=True)
        print(json.dumps({"type":"provider.usage","payload":{"input_tokens":4,"output_tokens":5}}), flush=True)
        print(json.dumps({"type":"complete","payload":{"text":"done","session_id":"s1","tokens_in":4,"tokens_out":5}}), flush=True)
        """,
    )
    events = []
    old = os.environ.get("FACTORY_GITHUB_TOKEN")
    os.environ["FACTORY_GITHUB_TOKEN"] = "secret"
    try:
        result = SDKRunner(worker_command=[python, str(script)]).run(
            ProviderRequest("codex", "m", "$(touch SHOULD_NOT_EXIST); 'quoted'", str(tmp_path)),
            lambda typ, payload: events.append((typ, payload)),
        )
    finally:
        if old is None:
            os.environ.pop("FACTORY_GITHUB_TOKEN", None)
        else:
            os.environ["FACTORY_GITHUB_TOKEN"] = old
    assert result == ProviderResult("done", "s1", None, 4, 5)
    assert [event[0] for event in events] == ["provider.session", "assistant.message", "provider.usage"]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_parent_rejects_worker_error(tmp_path):
    python, script = _worker(
        tmp_path,
        """
        import json
        print(json.dumps({"type":"error","payload":{"message":"missing optional sdk"}}), flush=True)
        raise SystemExit(3)
        """,
    )
    with pytest.raises(ProviderError, match="missing optional sdk"):
        SDKRunner(worker_command=[python, str(script)]).run(
            ProviderRequest("claude", "m", "p", str(tmp_path)), lambda *_: None
        )


def test_timeout_terminates_worker(tmp_path):
    python, script = _worker(
        tmp_path,
        """
        import time
        time.sleep(10)
        """,
    )
    with pytest.raises(ProviderTimeout):
        SDKRunner(worker_command=[python, str(script)]).run(
            ProviderRequest("dsh", "m", "p", str(tmp_path), timeout_s=1), lambda *_: None
        )


def test_cancel_terminates_worker(tmp_path):
    python, script = _worker(
        tmp_path,
        """
        import time
        time.sleep(10)
        """,
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProviderCancelled):
        SDKRunner(worker_command=[python, str(script)]).run(
            ProviderRequest("dsh", "m", "p", str(tmp_path)), lambda *_: None, cancelled
        )


def test_available_has_stable_provider_shape():
    available = SDKRunner.available()
    assert {item["id"] for item in available} == {"claude", "codex", "dsh"}
    assert all(set(item) == {"id", "installed", "detail"} for item in available)


def test_dsh_read_only_refuses_before_optional_import(monkeypatch, tmp_path):
    # This invokes the worker adapter directly to pin the safety guarantee
    # without installing or making a paid provider call.
    from factory.control.providers import _run_dsh

    with pytest.raises(ProviderError, match="verified read-only"):
        _run_dsh(ProviderRequest("dsh", "m", "p", str(tmp_path), read_only=True), lambda *_: None)


def test_codex_adapter_uses_exact_sandbox_and_never_retries_prompt(monkeypatch, tmp_path):
    from factory.control.providers import _run_codex

    calls = []
    mod = types.ModuleType("openai_codex")

    class ApprovalMode:
        deny_all = "deny_all"

    class Sandbox:
        read_only = "read_only"
        workspace_write = "workspace_write"

    class Result:
        status = "completed"
        error = None
        items = []
        usage = None
        final_response = "codex answer"

    class Thread:
        id = "thread-1"

        def run(self, prompt, *, cwd, sandbox, approval_mode):
            calls.append((prompt, cwd, sandbox, approval_mode))
            raise TypeError("SDK internal failure")

    class Codex:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def thread_start(self, *, model, cwd, sandbox, approval_mode):
            assert model == "model"
            return Thread()

    mod.Codex = Codex
    mod.Sandbox = Sandbox
    mod.ApprovalMode = ApprovalMode
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    with pytest.raises(TypeError, match="SDK internal failure"):
        _run_codex(ProviderRequest("codex", "model", "prompt", str(tmp_path)), lambda *_: None)
    assert len(calls) == 1
    assert calls[0][1] == str(tmp_path.resolve())
    assert calls[0][2] == Sandbox.workspace_write
    assert calls[0][3] == ApprovalMode.deny_all


def test_codex_adapter_emits_final_text_when_items_have_no_assistant(monkeypatch, tmp_path):
    from factory.control.providers import _run_codex

    mod = types.ModuleType("openai_codex")

    class ApprovalMode:
        deny_all = "deny_all"

    class Sandbox:
        read_only = "read_only"
        workspace_write = "workspace_write"

    class Result:
        status = "completed"
        error = None
        items = []
        usage = None
        final_response = "final"

    class Thread:
        id = "thread-1"

        def run(self, prompt, **kwargs):
            return Result()

    class Codex:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def thread_start(self, **kwargs): return Thread()

    mod.Codex, mod.Sandbox, mod.ApprovalMode = Codex, Sandbox, ApprovalMode
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    events = []
    result = _run_codex(ProviderRequest("codex", "model", "prompt", str(tmp_path)), lambda *event: events.append(event))
    assert result.text == "final"
    assert [event[0] for event in events].count("assistant.message") == 1


def test_dsh_rejects_non_completed_finish_reason_and_does_not_duplicate_streams(monkeypatch, tmp_path):
    from factory.control.providers import _run_dsh

    mod = types.ModuleType("deepseek_harness")

    class Result:
        session_id = "session-1"
        finish_reason = {"kind": "max-tokens"}
        final_response = "partial"
        events = [{"type": "assistant.message", "text": "partial"}]
        notifications = [{"type": "assistant.message", "text": "partial"}]

    class Harness:
        def __init__(self, **kwargs):
            assert kwargs["cwd"] == str(tmp_path.resolve())
            assert kwargs["dsh_home"] == str((tmp_path / "dsh").resolve())
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def run(self, prompt): return Result()

    mod.DeepSeekHarness = Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", mod)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    with pytest.raises(ProviderError, match="did not complete"):
        _run_dsh(ProviderRequest("dsh", "model", "prompt", str(tmp_path)), lambda *_: None)


def test_claude_pretool_hook_gates_auto_approved_paths(monkeypatch, tmp_path):
    from factory.control.providers import _run_claude

    mod = types.ModuleType("claude_agent_sdk")
    captured = {}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.__dict__.update(kwargs)

    class HookMatcher:
        def __init__(self, *, matcher=None, hooks=None, **kwargs):
            self.matcher, self.hooks = matcher, hooks or []

    class PermissionResult:
        def __init__(self, **kwargs): self.kwargs = kwargs

    class AssistantMessage:
        content = []
        session_id = "s"

    class ResultMessage:
        is_error = False
        result = "done"
        session_id = "s"
        total_cost_usd = None
        usage = None

    async def query(*, prompt, options):
        hook = options.hooks["PreToolUse"][0].hooks[0]
        outside = await hook({"tool_name": "Write", "tool_input": {"file_path": "/tmp/outside.py"}}, "id", {})
        secret = await hook({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / ".env")}}, "id", {})
        inside = await hook({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "src.py")}}, "id", {})
        assert outside["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert secret["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert inside["hookSpecificOutput"]["permissionDecision"] == "allow"
        yield AssistantMessage()
        yield ResultMessage()

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.HookMatcher = HookMatcher
    mod.PermissionResultAllow = PermissionResult
    mod.PermissionResultDeny = PermissionResult
    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    result = _run_claude(ProviderRequest("claude", "model", "prompt", str(tmp_path)), lambda *_: None)
    assert result.text == "done"
    assert captured["tools"] == ["Read", "Glob", "Grep", "Write", "Edit"]


def test_claude_read_only_tools_exclude_writes_and_glob_traversal(monkeypatch, tmp_path):
    from factory.control.providers import _run_claude

    mod = types.ModuleType("claude_agent_sdk")
    captured = {}

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.__dict__.update(kwargs)

    class HookMatcher:
        def __init__(self, *, matcher=None, hooks=None, **kwargs): self.hooks = hooks or []

    class PermissionResult:
        def __init__(self, **kwargs): pass

    class ResultMessage:
        is_error = False
        result = "read"
        session_id = "s"
        total_cost_usd = None
        usage = None

    async def query(*, prompt, options):
        hook = options.hooks["PreToolUse"][0].hooks[0]
        write = await hook({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x")}}, None, {})
        traversal = await hook({"tool_name": "Glob", "tool_input": {"path": "../*"}}, None, {})
        assert write["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert traversal["hookSpecificOutput"]["permissionDecision"] == "deny"
        yield ResultMessage()

    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.HookMatcher = HookMatcher
    mod.PermissionResultAllow = PermissionResult
    mod.PermissionResultDeny = PermissionResult
    mod.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    result = _run_claude(ProviderRequest("claude", "model", "prompt", str(tmp_path), read_only=True), lambda *_: None)
    assert result.text == "read"
    assert captured["tools"] == ["Read", "Glob", "Grep"]
