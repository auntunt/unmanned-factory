import json
import os
from enum import Enum
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def no_host_terminal_probe(monkeypatch):
    from factory.control import claude_terminal
    monkeypatch.setattr(claude_terminal, 'available', lambda: False)


from factory.control.providers import (
    ProviderCancelled,
    ProviderRequest,
    ProviderResult,
    ProviderTimeout,
    ProviderError,
    SDKRunner,
)


class _IsolatedCodexClient:
    """Minimal typed-transport double for pre-dispatch isolation checks."""

    def request(self, method, params, *, response_model):
        if method == "config/read":
            return types.SimpleNamespace(
                config=types.SimpleNamespace(features={"apps": False, "plugins": False, "code_mode_host": True, "unified_exec": False, "shell_snapshot": False, "shell_tool": True}, hooks={})
            )
        if method == "mcpServerStatus/list":
            return types.SimpleNamespace(data=[], next_cursor=None)
        if method == "skills/list":
            return types.SimpleNamespace(data=[])
        raise AssertionError(method)


def _worker(tmp_path: Path, body: str) -> tuple[str, Path]:
    script = tmp_path / "fake_sdk_worker.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return sys.executable, script


def test_worker_env_removes_desktop_control_channels_but_keeps_codex_home_and_proxy(monkeypatch):
    from factory.control.providers import _worker_env

    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/private/desktop.sock")
    monkeypatch.setenv("CODEX_PERMISSION_PROFILE", "desktop-profile")
    monkeypatch.setenv("CODEX_SESSION_ID", "desktop-session")
    # This mirrors a Unix environment value decoded with surrogateescape.
    monkeypatch.setenv("_", "broken\udcff-python")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("WORKSPACE_LABEL", "自动化构建")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-auth")

    env = _worker_env()

    assert "CODEX_APP_TOOLS_PIPE_PATH" not in env
    assert "CODEX_PERMISSION_PROFILE" not in env
    assert "CODEX_SESSION_ID" not in env
    assert "_" not in env
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert env["WORKSPACE_LABEL"] == "自动化构建"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["OPENAI_API_KEY"] == "provider-auth"


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


def test_safe_json_serializes_sdk_enum_to_its_public_value():
    from factory.control.providers import _safe_json

    class PatchStatus(Enum):
        completed = "completed"

    assert _safe_json({"status": PatchStatus.completed}) == {"status": "completed"}


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
        def __init__(self, config):
            self.config = config
            self._client = _IsolatedCodexClient()
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
    mod.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    monkeypatch.setattr("factory.control.providers._verify_codex_isolation", lambda *_: None)
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
        def __init__(self, config):
            self.config = config
            self._client = _IsolatedCodexClient()
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def thread_start(self, **kwargs): return Thread()

    mod.Codex, mod.Sandbox, mod.ApprovalMode = Codex, Sandbox, ApprovalMode
    mod.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    monkeypatch.setattr("factory.control.providers._verify_codex_isolation", lambda *_: None)
    events = []
    result = _run_codex(ProviderRequest("codex", "model", "prompt", str(tmp_path)), lambda *event: events.append(event))
    assert result.text == "final"
    assert [event[0] for event in events].count("assistant.message") == 1


def test_codex_adapter_reads_installed_cached_input_token_field(monkeypatch, tmp_path):
    from factory.control.providers import _run_codex

    mod = types.ModuleType("openai_codex")

    class ApprovalMode: deny_all = "deny_all"
    class Sandbox: read_only = "read_only"; workspace_write = "workspace_write"
    class Result:
        status = "completed"; error = None; items = []; final_response = "final"
        usage = {"total": {"input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 9}}
    class Thread:
        id = "thread-1"
        def run(self, prompt, **kwargs): return Result()
    class Codex:
        def __init__(self, config):
            self.config = config
            self._client = _IsolatedCodexClient()
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def thread_start(self, **kwargs): return Thread()

    mod.Codex, mod.Sandbox, mod.ApprovalMode = Codex, Sandbox, ApprovalMode
    mod.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    monkeypatch.setattr("factory.control.providers._verify_codex_isolation", lambda *_: None)
    result = _run_codex(ProviderRequest("codex", "model", "prompt", str(tmp_path)), lambda *_: None)
    assert (result.tokens_in, result.tokens_out, result.cached_input_tokens) == (12, 4, 9)


def test_codex_adapter_streams_public_turn_notifications_without_replaying_result_items(monkeypatch, tmp_path):
    from factory.control.providers import _run_codex

    mod = types.ModuleType("openai_codex")

    class ApprovalMode: deny_all = "deny_all"
    class Sandbox: read_only = "read_only"; workspace_write = "workspace_write"

    notifications = [
        {"method": "item/reasoning/textDelta", "payload": {"delta": "private thinking"}},
        {"method": "item/agentMessage/delta", "payload": {"delta": "one token"}},
        {"method": "item/started", "payload": {"item": {
            "id": "command-1", "type": "commandExecution", "command": "echo done"}}},
        {"method": "item/completed", "payload": {"item": {
            "id": "command-1", "type": "commandExecution", "command": "echo done",
            "aggregated_output": "done\n", "exit_code": 0}}},
        {"method": "thread/tokenUsage/updated", "payload": {"token_usage": {
            "total": {"input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 9}}}},
        {"method": "item/completed", "payload": {"item": {
            "id": "message-1", "type": "agentMessage", "phase": "final_answer", "text": "finished"}}},
        {"method": "turn/completed", "payload": {"turn": {
            "id": "turn-1", "status": "completed", "error": None}}},
    ]

    class Turn:
        def stream(self):
            return iter(notifications)

    class Thread:
        id = "thread-1"
        run_called = False

        def turn(self, prompt, **kwargs):
            assert prompt == "prompt"
            assert kwargs["approval_mode"] == ApprovalMode.deny_all
            return Turn()

        def run(self, *args, **kwargs):
            self.run_called = True
            raise AssertionError("stream-capable SDK must not use thread.run")

    thread = Thread()

    class Codex:
        def __init__(self, config):
            self.config = config
            self._client = _IsolatedCodexClient()
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def thread_start(self, **kwargs): return thread

    mod.Codex, mod.Sandbox, mod.ApprovalMode = Codex, Sandbox, ApprovalMode
    mod.CodexConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    monkeypatch.setattr("factory.control.providers._verify_codex_isolation", lambda *_: None)
    events = []
    result = _run_codex(ProviderRequest("codex", "model", "prompt", str(tmp_path)),
                        lambda *event: events.append(event))

    assert result == ProviderResult("finished", "thread-1", None, 12, 4, 9)
    assert not thread.run_called
    assert [event[0] for event in events].count("tool.call") == 1
    assert [event[0] for event in events].count("tool.result") == 1
    assert [event[0] for event in events].count("provider.usage") == 1
    assert [event[0] for event in events].count("assistant.message") == 1
    assert "provider.raw" not in [event[0] for event in events]
    assert "private thinking" not in str(events)
    assert "one token" not in str(events)


def test_codex_isolation_overrides_disable_ambient_mcp_without_copying_credentials(monkeypatch, tmp_path):
    from factory.control.providers import _codex_isolation_overrides

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """[mcp_servers.personal_tools]
command = "real-command"
env = { API_TOKEN = "must-not-copy" }
""",
        encoding="utf-8",
    )
    project_config = tmp_path / ".codex"
    project_config.mkdir()
    (project_config / "config.toml").write_text(
        """[mcp_servers.project-tools]
url = "https://example.invalid/mcp"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    overrides = _codex_isolation_overrides(str(tmp_path))
    rendered = "\n".join(overrides)

    assert "features.apps=false" in overrides
    assert "features.plugins=false" in overrides
    assert "features.remote_plugin=false" in overrides
    assert "features.code_mode_host=true" in overrides
    assert "features.unified_exec=false" in overrides
    assert "features.shell_snapshot=false" in overrides
    assert "features.shell_tool=true" in overrides
    assert '"personal_tools"={command="/__factory_mcp_disabled__",enabled=false}' in rendered
    assert '"project-tools"={command="/__factory_mcp_disabled__",enabled=false}' in rendered
    assert "real-command" not in rendered
    assert "must-not-copy" not in rendered
    assert "example.invalid" not in rendered
    assert 'hooks."PreToolUse"=[]' in overrides


def test_codex_isolation_rejects_active_mcp_tools_before_dispatch(tmp_path):
    from factory.control.providers import _verify_codex_isolation

    class Client:
        def request(self, method, params, *, response_model):
            if method == "config/read":
                return types.SimpleNamespace(config=types.SimpleNamespace(features={"apps": False, "plugins": False, "code_mode_host": True, "unified_exec": False, "shell_snapshot": False, "shell_tool": True}, hooks={}))
            if method == "mcpServerStatus/list":
                if "cursor" not in params:
                    return types.SimpleNamespace(data=[], next_cursor="second-page")
                assert params["cursor"] == "second-page"
                return types.SimpleNamespace(data=[types.SimpleNamespace(name="ambient", tools=["tool"])], next_cursor=None)
            assert method == "skills/list"
            return types.SimpleNamespace(data=[])

    with pytest.raises(ProviderError, match="active MCP tools"):
        _verify_codex_isolation(types.SimpleNamespace(_client=Client()), str(tmp_path))


def test_codex_isolation_refuses_to_dispatch_without_app_server_transport(tmp_path):
    from factory.control.providers import _verify_codex_isolation

    with pytest.raises(ProviderError, match="transport is unavailable"):
        _verify_codex_isolation(object(), str(tmp_path))


@pytest.mark.parametrize('bad', [True, False, -1, 1.5, 'NaN', '1.5'])
def test_invalid_sdk_usage_is_not_reported_as_an_integer(bad):
    from factory.control.providers import _usage_numbers
    assert _usage_numbers({'input_tokens': bad, 'output_tokens': 4}) == (None, 4)


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
    from factory.control import claude_terminal
    monkeypatch.setattr(claude_terminal, 'available', lambda: True)
    monkeypatch.setattr(claude_terminal, 'create_server', lambda workspace: {'type': 'sdk', 'name': 'project'})

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
        terminal = await hook({'tool_name': 'mcp__project__run_command', 'tool_input': {'command': 'python3 -m unittest'}}, 'id', {})
        assert terminal['hookSpecificOutput']['permissionDecision'] == 'allow'
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
    assert captured['allowed_tools'] == ['mcp__project__run_command']
    assert captured['system_prompt']['preset'] == 'claude_code'
    assert str(tmp_path) in captured['system_prompt']['append']


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


def test_claude_glob_accepts_absolute_workspace_but_not_escape(tmp_path):
    from factory.control.providers import _claude_tool_allowed
    assert _claude_tool_allowed('Glob', {'path': str(tmp_path), 'pattern': '**/*.py'}, tmp_path, False)
    assert not _claude_tool_allowed('Glob', {'path': str(tmp_path.parent)}, tmp_path, False)
    assert not _claude_tool_allowed('Glob', {'path': '../'}, tmp_path, False)
