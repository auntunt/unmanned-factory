"""Optional provider SDK adapters with a bounded child-process transport.

The control plane imports this module without importing any provider SDK.  Each
SDK is loaded by :mod:`factory.control.sdk_worker` in a fresh process so an
SDK crash, native runtime, or accidental global state cannot take down the
factory process.
"""
from __future__ import annotations

import json
import inspect
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    model: str
    prompt: str
    workspace: str
    session_id: str | None = None
    timeout_s: int = 600
    read_only: bool = False


@dataclass(frozen=True)
class ProviderResult:
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


class ProviderError(RuntimeError):
    """A provider was unavailable, failed, or returned an invalid result."""


class ProviderTimeout(ProviderError):
    """The provider exceeded the request timeout."""


class ProviderCancelled(ProviderError):
    """The caller cancelled the provider process."""


_PROVIDER_IMPORTS = {
    "claude": ("claude_agent_sdk", "Claude Agent SDK (claude-agent-sdk)"),
    "codex": ("openai_codex", "OpenAI Codex SDK (openai-codex)"),
    "dsh": ("deepseek_harness", "DeepSeek Harness SDK (deepseek-harness-sdk)"),
}
# Control-plane/bootstrap credentials must never cross the SDK process boundary.
# Provider credentials (for example ANTHROPIC_API_KEY) intentionally remain
# available because SDK authentication is provider-owned.
_STRIPPED_ENV_KEYS = {
    "FACTORY_GITHUB_TOKEN",
    "FACTORY_WEBHOOK_SECRET",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}

Emit = Callable[[str, dict[str, Any]], None]
_MAX_JSONL_LINE = 1_048_576


def _reasoning_type(value: Any) -> bool:
    if isinstance(value, Mapping):
        typ = value.get("type") or value.get("kind") or value.get("block_type")
        return isinstance(typ, str) and any(word in typ.lower() for word in ("thinking", "reasoning"))
    name = type(value).__name__.lower()
    return "thinking" in name or "reasoning" in name


def _safe_json(value: Any, *, _depth: int = 0) -> Any:
    """Turn arbitrary SDK objects into bounded JSON suitable for persistence.

    The root persistence layer applies its own redaction policy.  This first
    pass prevents unserialisable objects and huge SDK payloads from blocking
    the JSONL control channel, while redacting obvious credential fields.
    """
    if _depth > 8:
        return "<depth-limit>"
    if _reasoning_type(value):
        return {"type": "reasoning", "redacted": True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 32_000 else value[:32_000] + "...[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower().replace("-", "_")
            if (
                lowered in {"thinking", "reasoning", "thought", "chain_of_thought"}
                or "reasoning_content" in lowered
                or lowered.endswith("_thinking")
            ):
                continue
            if any(token in lowered for token in ("api_key", "access_token", "refresh_token", "secret", "password", "authorization")):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = _safe_json(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item, _depth=_depth + 1) for item in list(value)[:1000]]
    # Pydantic models, dataclasses, and SDK response objects commonly expose
    # model_dump/asdict/attributes.  Avoid invoking arbitrary callables.
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return _safe_json(fn(), _depth=_depth + 1)
            except Exception:
                pass
    try:
        attrs = vars(value)
    except TypeError:
        return str(value)[:32_000]
    return _safe_json(attrs, _depth=_depth + 1)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
        # SDK wire payloads may use camelCase while Python objects use snake.
        camel = name.split("_")[0] + "".join(p.title() for p in name.split("_")[1:])
        return obj.get(camel, default)
    return getattr(obj, name, default)


def _event_class(obj: Any) -> str:
    typ = _value(obj, "type")
    if typ is not None:
        return str(getattr(typ, "value", typ))
    return type(obj).__name__


def _usage_numbers(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    incoming = _value(usage, "input_tokens")
    outgoing = _value(usage, "output_tokens")
    if incoming is None:
        incoming = _value(usage, "prompt_tokens")
    if outgoing is None:
        outgoing = _value(usage, "completion_tokens")
    try:
        incoming = int(incoming) if incoming is not None else None
    except (TypeError, ValueError):
        incoming = None
    try:
        outgoing = int(outgoing) if outgoing is not None else None
    except (TypeError, ValueError):
        outgoing = None
    return incoming, outgoing


def _emit_session(emit: Emit, session_id: Any) -> str | None:
    if session_id is None:
        return None
    session = str(session_id)
    emit("provider.session", {"session_id": session})
    return session


def _emit_generic_event(event: Any, emit: Emit) -> None:
    """Normalize a provider notification when its shape is recognisable."""
    kind = _event_class(event).lower().replace("_", ".")
    payload = _safe_json(event)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    if "reasoning" in kind or "thinking" in kind:
        # Hidden thought must never be exposed as a provider event.
        return
    if any(token in kind for token in ("assistant", "agent.message", "message")):
        text = _value(event, "text") or _value(event, "content") or _value(event, "message")
        if isinstance(text, str) and text:
            emit("assistant.message", {"text": text})
            return
    if "tool" in kind and any(token in kind for token in ("call", "use", "start")):
        emit("tool.call", payload)
        return
    if "tool" in kind and any(token in kind for token in ("result", "output", "end", "complete")):
        emit("tool.result", payload)
        return
    if "usage" in kind:
        emit("provider.usage", payload)
        return
    if "session" in kind:
        sid = _value(event, "session_id") or _value(event, "sessionId")
        if sid is not None:
            emit("provider.session", {"session_id": str(sid)})
            return
    emit("provider.raw", {"event": payload})


def _claude_tool_path(tool_name: str, input_data: Mapping[str, Any], workspace: Path) -> Path | None:
    """Resolve Claude file-tool input without permitting path escape."""
    field = "file_path" if tool_name in {"Read", "Write", "Edit"} else "path"
    raw = input_data.get(field)
    if raw is None and tool_name in {"Glob", "Grep"}:
        raw = "."
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw)
    if tool_name == "Glob" and (candidate.is_absolute() or ".." in candidate.parts):
        return None
    if not candidate.is_absolute():
        candidate = workspace / candidate
    # Glob patterns are checked at the nearest concrete parent.  The tool
    # still receives the original pattern after this boundary check.
    while any(char in candidate.name for char in "*?["):
        candidate = candidate.parent
    return candidate.resolve(strict=False)


def _claude_tool_allowed(tool_name: str, input_data: Mapping[str, Any], workspace: Path, read_only: bool) -> bool:
    # These are Claude Code's local inspection tools.  Anything that can write,
    # execute, or reach an external service remains denied without an approval
    # broker supplied by the root orchestrator.
    if tool_name not in {"Read", "Glob", "Grep", "Write", "Edit"}:
        return False
    if read_only and tool_name not in {"Read", "Glob", "Grep"}:
        return False
    path = _claude_tool_path(tool_name, input_data, workspace)
    if path is None:
        return False
    if tool_name in {"Write", "Edit"}:
        # Keep source workspaces usable while preventing VCS metadata and
        # credential/config tampering. This runs for hook and callback paths.
        try:
            relative_parts = path.relative_to(workspace).parts
        except ValueError:
            return False
        parts = {part.lower() for part in relative_parts}
        name = path.name.lower()
        if ".git" in parts or name == ".git" or name == ".env" or name.startswith(".env."):
            return False
        if any(token in name for token in ("credential", "secret", "password")):
            return False
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            return False
    try:
        path.relative_to(workspace)
    except ValueError:
        return False
    return True


def _run_claude(req: ProviderRequest, emit: Emit) -> ProviderResult:
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
            HookMatcher,
            PermissionResultAllow,
            PermissionResultDeny,
            query,
        )
    except ImportError as exc:
        raise ProviderError("claude SDK is not installed; install claude-agent-sdk") from exc

    workspace = Path(req.workspace).resolve()

    async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        if _claude_tool_allowed(tool_name, input_data, workspace, req.read_only):
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(
            message="No approval broker is available in the SDK worker; tool call denied",
            interrupt=True,
        )

    async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """Gate every tool call, including calls auto-approved by SDK settings."""
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input")
        if not isinstance(tool_input, Mapping):
            tool_input = {}
        allowed = _claude_tool_allowed(tool_name, tool_input, workspace, req.read_only)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    "workspace-bounded file operation"
                    if allowed else "tool or path denied by SDK worker policy"
                ),
            }
        }

    options_kwargs: dict[str, Any] = {
        "model": req.model or None,
        "cwd": str(workspace),
        "tools": ["Read", "Glob", "Grep"] if req.read_only else ["Read", "Glob", "Grep", "Write", "Edit"],
        "permission_mode": "default",
        "can_use_tool": can_use_tool,
        "hooks": {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
        # Ignore ambient user/project settings and MCP/plugin configuration.
        "setting_sources": [],
        "strict_mcp_config": True,
    }
    if req.session_id:
        options_kwargs["resume"] = req.session_id

    import asyncio

    text_parts: list[str] = []
    result: Any = None
    session_id: str | None = None
    tokens_in = tokens_out = None
    cost_usd: float | None = None

    async def consume() -> None:
        nonlocal result, session_id, tokens_in, tokens_out, cost_usd
        options = ClaudeAgentOptions(**options_kwargs)
        async for message in query(prompt=req.prompt, options=options):
            name = _event_class(message)
            if name == "AssistantMessage" or "assistant" in name.lower():
                content = _value(message, "content", [])
                for block in content if isinstance(content, (list, tuple)) else [content]:
                    block_name = _event_class(block)
                    if "thinking" in block_name.lower():
                        continue
                    if "text" in block_name.lower() or block_name == "TextBlock":
                        text = _value(block, "text")
                        if text:
                            text_parts.append(str(text))
                            emit("assistant.message", {"text": str(text)})
                    elif "tooluse" in block_name.lower() or "tool_use" in block_name.lower():
                        emit("tool.call", _safe_json({
                            "id": _value(block, "id"),
                            "name": _value(block, "name"),
                            "input": _value(block, "input"),
                        }))
                    elif "toolresult" in block_name.lower() or "tool_result" in block_name.lower():
                        emit("tool.result", _safe_json({
                            "tool_use_id": _value(block, "tool_use_id"),
                            "content": _value(block, "content"),
                            "is_error": _value(block, "is_error"),
                        }))
                    else:
                        _emit_generic_event(block, emit)
                sid = _value(message, "session_id")
                if sid is not None:
                    session_id = _emit_session(emit, sid) or session_id
            elif name == "ResultMessage" or "resultmessage" in name.lower():
                result = message
                sid = _value(message, "session_id")
                if sid is not None:
                    session_id = _emit_session(emit, sid) or session_id
                cost = _value(message, "total_cost_usd")
                if cost is not None:
                    try:
                        cost_usd = float(cost)
                    except (TypeError, ValueError):
                        pass
                tokens_in, tokens_out = _usage_numbers(_value(message, "usage"))
            elif name == "SystemMessage" or "systemmessage" in name.lower():
                data = _value(message, "data", {})
                sid = _value(data, "session_id")
                if sid is not None:
                    session_id = _emit_session(emit, sid) or session_id
            else:
                _emit_generic_event(message, emit)

    asyncio.run(consume())
    if result is not None and bool(_value(result, "is_error", False)):
        detail = _value(result, "result") or _value(result, "errors") or "Claude returned an error"
        raise ProviderError(str(detail))
    final = _value(result, "result") if result is not None else None
    if not isinstance(final, str) or not final:
        final = "".join(text_parts)
    if not final:
        raise ProviderError("Claude SDK completed without an assistant result")
    return ProviderResult(final, session_id, cost_usd, tokens_in, tokens_out)


def _codex_item(item: Any, emit: Emit) -> bool:
    root = _value(item, "root", item)
    typ = str(_value(root, "type", type(root).__name__))
    low = typ.lower()
    if "reasoning" in low:
        return
    if "agentmessage" in low or low in {"assistant.message", "assistant_message"}:
        text = _value(root, "text")
        if text:
            emit("assistant.message", {"text": str(text)})
            return True
        return False
    if "commandexecution" in low or "shell" in low:
        ident = _value(root, "id")
        emit("tool.call", _safe_json({"id": ident, "name": "command", "command": _value(root, "command")}))
        emit("tool.result", _safe_json({"id": ident, "output": _value(root, "aggregated_output"), "exit_code": _value(root, "exit_code")}))
        return False
    if "mcp" in low or "dynamictool" in low:
        emit("tool.call", _safe_json(root))
        emit("tool.result", _safe_json(root))
        return False
    if "filechange" in low:
        emit("tool.call", _safe_json({"id": _value(root, "id"), "name": "file_change", "changes": _value(root, "changes")}))
        emit("tool.result", _safe_json({"id": _value(root, "id"), "status": _value(root, "status")}))
        return False
    _emit_generic_event(root, emit)
    return False


def _accepted_kwargs(function: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Filter compatibility kwargs without retrying an invoked SDK call."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


def _run_codex(req: ProviderRequest, emit: Emit) -> ProviderResult:
    try:
        from openai_codex import Codex, Sandbox  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderError("codex SDK is not installed; install openai-codex") from exc

    # ApprovalMode.deny_all is required: a build that cannot express the
    # no-approval policy must fail rather than silently use an interactive or
    # auto-review default.
    try:
        from openai_codex import ApprovalMode  # type: ignore[import-not-found]
    except ImportError:
        raise ProviderError("installed codex SDK lacks required ApprovalMode.deny_all")
    sandbox = Sandbox.read_only if req.read_only else Sandbox.workspace_write
    start_kwargs: dict[str, Any] = {
        "model": req.model,
        "sandbox": sandbox,
        "cwd": str(Path(req.workspace).resolve()),
    }
    start_kwargs["approval_mode"] = ApprovalMode.deny_all
    with Codex() as codex:
        if req.session_id:
            resume = getattr(codex, "thread_resume", None)
            if not callable(resume):
                raise ProviderError("installed codex SDK cannot resume a session")
            thread = resume(req.session_id, **_accepted_kwargs(resume, start_kwargs))
        else:
            thread = codex.thread_start(**_accepted_kwargs(codex.thread_start, start_kwargs))
        # thread.run is the documented stable API.  Current builds return a
        # TurnResult containing item-level notifications and usage.
        run_kwargs = {"cwd": str(Path(req.workspace).resolve()), "sandbox": sandbox}
        run_kwargs["approval_mode"] = ApprovalMode.deny_all
        # Filter an older SDK's keyword surface before invocation. Retrying on
        # TypeError would execute a paid prompt twice when the SDK itself fails.
        result = thread.run(req.prompt, **_accepted_kwargs(thread.run, run_kwargs))
    session_id = _value(thread, "id") or req.session_id
    if session_id is not None:
        session_id = _emit_session(emit, session_id) or session_id
    items = _value(result, "items", []) or []
    assistant_emitted = False
    for item in items:
        if _codex_item(item, emit):
            assistant_emitted = True
    usage = _value(result, "usage")
    tokens_in, tokens_out = _usage_numbers(_value(usage, "total", usage))
    if usage is not None:
        emit("provider.usage", _safe_json(usage))
    status = str(getattr(_value(result, "status"), "value", _value(result, "status", "completed")))
    error = _value(result, "error")
    if error is not None or status.lower() in {"failed", "error", "cancelled", "canceled"}:
        raise ProviderError(str(_value(error, "message", error) or f"Codex turn {status}"))
    text = _value(result, "final_response")
    if not isinstance(text, str) or not text:
        raise ProviderError("Codex SDK completed without an assistant result")
    if not assistant_emitted:
        emit("assistant.message", {"text": text})
    return ProviderResult(text, session_id, None, tokens_in, tokens_out)


def _run_dsh(req: ProviderRequest, emit: Emit) -> ProviderResult:
    if req.read_only:
        raise ProviderError("dsh read_only is refused: DeepSeek Harness SDK has no verified read-only capability")
    try:
        from deepseek_harness import DeepSeekHarness  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderError("dsh SDK is not installed; install deepseek-harness-sdk") from exc
    dsh_home = os.environ.get("DSH_HOME", "").strip()
    if not dsh_home:
        raise ProviderError("dsh requires an explicit non-empty DSH_HOME")
    kwargs: dict[str, Any] = {
        "dsh_home": str(Path(dsh_home).resolve()),
        "cwd": str(Path(req.workspace).resolve()),
        "model": req.model,
    }
    # The provider route is explicit where supported; leave it unset if a
    # pinned older SDK does not expose the keyword (never silently downgrade
    # the required cwd/home arguments).
    kwargs["provider"] = "deepseek-official"
    try:
        with DeepSeekHarness(**kwargs) as harness:
            result = harness.run(req.prompt, session_id=req.session_id) if req.session_id else harness.run(req.prompt)
    except TypeError as exc:
        # Provider/model are documented current arguments; a mismatch is a
        # configuration failure, not permission to retry with an unsafe default.
        raise ProviderError(f"dsh SDK API mismatch: {exc}") from exc
    session_id = _value(result, "session_id")
    if session_id is not None:
        session_id = _emit_session(emit, session_id) or str(session_id)
    finish_reason = _value(result, "finish_reason")
    finish_kind = _value(finish_reason, "kind", finish_reason)
    if finish_kind != "completed":
        raise ProviderError(f"dsh run did not complete successfully: {finish_kind!r}")
    assistant_emitted = False

    def emit_dsh(typ: str, payload: dict[str, Any]) -> None:
        nonlocal assistant_emitted
        assistant_emitted = assistant_emitted or typ == "assistant.message"
        emit(typ, payload)

    # `events` is the root-session collection; notifications also includes
    # descendants and can repeat root events. Prefer events to avoid doubles.
    events = _value(result, "events", []) or []
    if not events:
        events = _value(result, "notifications", []) or []
    for event in events:
        _emit_generic_event(event, emit_dsh)
    text = _value(result, "final_response")
    if not isinstance(text, str) or not text:
        raise ProviderError("dsh SDK completed without an assistant result")
    if not assistant_emitted:
        emit("assistant.message", {"text": text})
    return ProviderResult(text, session_id)


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if key in _STRIPPED_ENV_KEYS or (
            upper.startswith("FACTORY_")
            and any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH"))
        ):
            env.pop(key, None)
    return env


class SDKRunner:
    """Run an optional provider SDK in an isolated, bounded worker."""

    def __init__(
        self,
        *,
        python: str | None = None,
        worker_module: str = "factory.control.sdk_worker",
        worker_command: Sequence[str] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.python = python or sys.executable
        self.worker_module = worker_module
        self.worker_command = tuple(worker_command) if worker_command else None
        self._popen = popen_factory

    @staticmethod
    def available() -> list[dict[str, Any]]:
        import importlib.util

        available: list[dict[str, Any]] = []
        for provider, (module, label) in _PROVIDER_IMPORTS.items():
            try:
                installed = importlib.util.find_spec(module) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                installed = False
            detail = f"{label} installed" if installed else f"{label} not installed"
            if provider == "dsh" and installed and not os.environ.get("DSH_HOME", "").strip():
                detail += "; DSH_HOME is not set"
            available.append({"id": provider, "installed": installed, "detail": detail})
        return available

    def run(
        self,
        request: ProviderRequest,
        emit: Emit,
        cancel: threading.Event | None = None,
    ) -> ProviderResult:
        if request.provider not in _PROVIDER_IMPORTS:
            raise ProviderError(f"unknown provider: {request.provider!r}")
        if not request.model:
            raise ProviderError("provider model is required")
        if not request.workspace:
            raise ProviderError("provider workspace is required")
        if request.timeout_s <= 0:
            raise ProviderError("provider timeout_s must be positive")
        # One request is one JSONL line.  Reject oversized prompts before
        # writing to a pipe: a blocking write can otherwise deadlock while
        # the worker waits for its terminating newline.
        payload = json.dumps(asdict(request), separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(payload) > _MAX_JSONL_LINE:
            raise ProviderError(f"provider request exceeds {_MAX_JSONL_LINE} byte JSONL limit")
        command = list(self.worker_command or (self.python, "-m", self.worker_module))
        try:
            proc = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_worker_env(),
                start_new_session=(os.name == "posix"),
                close_fds=True,
            )
        except OSError as exc:
            raise ProviderError(f"cannot launch provider worker: {exc}") from exc
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.close()
        except OSError as exc:
            self._stop(proc)
            raise ProviderError(f"cannot send provider request: {exc}") from exc

        result_payload: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None
        started = time.monotonic()
        buffers: dict[int, bytearray] = {}
        selector = selectors.DefaultSelector()
        for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ, label)
                buffers[stream.fileno()] = bytearray()
        normal_exit = False
        try:
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    self._stop(proc)
                    raise ProviderCancelled(f"provider {request.provider} cancelled")
                remaining = request.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    self._stop(proc)
                    raise ProviderTimeout(f"provider {request.provider} timed out after {request.timeout_s}s")
                events = selector.select(min(remaining, 0.25))
                if not events and proc.poll() is not None:
                    # Drain EOF notifications on the next iteration.
                    continue
                for key, _ in events:
                    stream = key.fileobj
                    try:
                        data = os.read(stream.fileno(), 64 * 1024)
                    except OSError:
                        data = b""
                    if not data:
                        selector.unregister(stream)
                        continue
                    buf = buffers[stream.fileno()]
                    buf.extend(data)
                    if len(buf) > _MAX_JSONL_LINE:
                        raise ProviderError(f"provider worker emitted a line over {_MAX_JSONL_LINE} bytes")
                    while b"\n" in buf:
                        line, _, rest = buf.partition(b"\n")
                        buf.clear()
                        buf.extend(rest)
                        if not line.strip():
                            continue
                        message = self._consume_line(line, key.data, emit)
                        if message is None:
                            continue
                        typ = message.get("type")
                        if typ == "complete" and isinstance(message.get("payload"), dict):
                            result_payload = message["payload"]
                        elif typ == "error" and isinstance(message.get("payload"), dict):
                            error_payload = message["payload"]
            # Process any non-newline tail as raw diagnostics, but never treat
            # it as a successful provider result.
            for fd, buf in buffers.items():
                if buf:
                    emit("provider.raw", {"stream": "worker", "text": bytes(buf).decode("utf-8", "replace")})
            normal_exit = True
        finally:
            selector.close()
            if not normal_exit:
                self._stop(proc)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        try:
            rc = proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._stop(proc)
            try:
                rc = proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("provider worker did not exit after termination") from exc
        if error_payload is not None:
            raise ProviderError(str(error_payload.get("message") or "provider worker failed"))
        if rc != 0:
            raise ProviderError(f"provider worker exited with status {rc}")
        if result_payload is None:
            raise ProviderError("provider worker exited without a result")
        try:
            return ProviderResult(
                text=str(result_payload["text"]),
                session_id=result_payload.get("session_id"),
                cost_usd=result_payload.get("cost_usd"),
                tokens_in=result_payload.get("tokens_in"),
                tokens_out=result_payload.get("tokens_out"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("provider worker returned an invalid result") from exc

    @staticmethod
    def _consume_line(line: bytes, stream: str, emit: Emit) -> dict[str, Any] | None:
        if len(line) > _MAX_JSONL_LINE:
            raise ProviderError(f"provider worker emitted a line over {_MAX_JSONL_LINE} bytes")
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            emit("provider.raw", {"stream": stream, "text": line.decode("utf-8", "replace")})
            return None
        if not isinstance(message, dict):
            raise ProviderError("provider worker emitted a non-object JSONL message")
        typ = message.get("type")
        payload = message.get("payload")
        if typ in {"assistant.message", "tool.call", "tool.result", "provider.session", "provider.usage", "provider.raw"}:
            emit(str(typ), payload if isinstance(payload, dict) else {"value": _safe_json(payload)})
        elif typ not in {"complete", "error"}:
            emit("provider.raw", {"stream": stream, "event": _safe_json(message)})
        return message

    @staticmethod
    def _stop(proc: Any) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
