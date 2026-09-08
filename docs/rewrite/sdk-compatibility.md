# Optional provider SDK compatibility

`factory.control.providers.SDKRunner` invokes one provider SDK per isolated child
process. The parent sends exactly one JSON request to
`python -m factory.control.sdk_worker`, reads JSONL events, and kills the whole
process group on cancellation or timeout. Provider SDKs are optional: a missing
package is reported by `SDKRunner.available()` and fails a run explicitly.

The adapter API is intentionally small:

```python
@dataclass(frozen=True)
class ProviderRequest:
    provider: str                 # claude, codex, or dsh
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
```

`SDKRunner.run(request, emit, cancel)` emits only normalized
`assistant.message`, `tool.call`, `tool.result`, `provider.session`,
`provider.usage`, and `provider.raw` events. Thinking or reasoning blocks are
never emitted. Unknown SDK notifications become bounded `provider.raw` data;
the root persistence layer remains responsible for its final redaction policy.

## Claude Agent SDK

Verified against the [Python Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/python):

```python
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    model="<model>",
    cwd="/absolute/workspace",
    permission_mode="default",
    can_use_tool=...,       # callback can deny unavailable approvals
    setting_sources=[],
    strict_mcp_config=True,
)
async for message in query(prompt="...", options=options):
    ...
```

The SDK returns typed `AssistantMessage` blocks and a final `ResultMessage`
with `session_id`, `result`, `total_cost_usd`, `usage`, and `is_error`. The
adapter resumes a supplied session with `resume`. A `PreToolUse` hook gates
every tool call, including SDK calls that would otherwise bypass the permission
callback. With no approval broker, the worker permits only local inspection in
read-only mode (`Read`, `Glob`, and `Grep`). In write mode it additionally
permits `Write` and `Edit` only for paths below the exact workspace, excluding
`.git`, `.env`, and credential files. Ambient settings, MCP servers, and
plugins are disabled through `setting_sources=[]` and `strict_mcp_config=True`.
Install `claude-agent-sdk` (pin a tested release in the deployment's optional
dependency set).

## OpenAI Codex Python SDK

Verified against the [stable Codex SDK guide](https://learn.chatgpt.com/docs/codex-sdk)
and the [Python SDK source](https://github.com/openai/codex/tree/main/sdk/python):

```python
from openai_codex import ApprovalMode, Codex, Sandbox

with Codex() as codex:
    thread = codex.thread_start(
        model="<model>",
        cwd="/absolute/workspace",
        sandbox=Sandbox.workspace_write,
        approval_mode=ApprovalMode.deny_all,
    )
    result = thread.run(
        "...",
        cwd="/absolute/workspace",
        sandbox=Sandbox.workspace_write,
        approval_mode=ApprovalMode.deny_all,
    )
```

The guide documents `Codex`, `Sandbox.read_only`,
`Sandbox.workspace_write`, and `thread.run()` returning a result with
`final_response`, collected items, and usage. Current stable source also
provides `ApprovalMode.deny_all` and `thread_resume(thread_id, ...)`; the
adapter requires `ApprovalMode.deny_all` and refuses session continuation when
the installed SDK cannot resume. The worker passes the absolute workspace as
`cwd` and maps `read_only` to `Sandbox.read_only`; write runs use
`Sandbox.workspace_write`. It never requests `Sandbox.full_access`.

Install `openai-codex` (pin a tested stable release in the deployment's optional
dependency set). Codex does not expose a Claude-style dollar cost in its
`TurnResult`, so `cost_usd` remains `None`; token usage is copied when present.

## DeepSeek Harness Python SDK

Verified against the [DeepSeek Harness Python SDK README](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md):

```python
from deepseek_harness import DeepSeekHarness

with DeepSeekHarness(
    dsh_home="/absolute/isolated-dsh-home",
    cwd="/absolute/workspace",
    provider="deepseek-official",
    model="<model>",
) as harness:
    result = harness.run("...", session_id="...")
```

Every launch requires an explicit, non-empty `DSH_HOME` (the runtime does not
discover `~/.dsh`). `cwd` is the agent workspace. The SDK returns
`RunResult(session_id, final_response, finish_reason, events, notifications)`.
The adapter normalizes its event and notification collections when available.
The SDK currently has no verified read-only/sandbox capability, so requests with
`read_only=True` fail before the SDK starts. This is deliberate: a provider
without a verified read-only guarantee must not be presented as read-only.

Install `deepseek-harness-sdk` (which supplies the matching
`deepseek-harness-runtime-bin` wheel), and configure a fresh explicit `DSH_HOME`
for each isolation domain as required by the deployment. `cost_usd` and token
counts are `None` unless the SDK exposes those values in a normalized result.

## Security and failure behavior

The worker environment strips `FACTORY_GITHUB_TOKEN`,
`FACTORY_WEBHOOK_SECRET`, `GH_TOKEN`, and `GITHUB_TOKEN`. Provider-owned
credentials may remain for SDK authentication. Requests are passed as JSON on
stdin, never interpolated into a shell command. Non-zero worker exits, SDK
import errors, missing `DSH_HOME`, unsupported read-only requests, provider
error results, timeout, and cancellation all raise a `ProviderError` subtype;
none can be converted into a successful `ProviderResult`.
