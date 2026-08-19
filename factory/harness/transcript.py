"""transcript 定位与解析。

原生 OTel 只导出元数据，不含对话内容；真正的审计钩子是
~/.claude/projects/<slug>/<session-id>.jsonl。

slug 由 cwd 生成且**有损**（`_` → `-`，中文目录整段变成 `-`），
从 cwd 反推 slug 不可靠，所以按 session-id 全局 glob。
"""

from __future__ import annotations

import json
from pathlib import Path

from factory.harness.base import ToolCall


def projects_root(home: Path | None = None) -> Path:
    """`<home>/.claude/projects`。home 省略时用宿主的 `~`。

    带 home 参数是给沙箱用的：那边 claude 的 $HOME 被指到一个每次执行独立的
    出口目录，transcript 落在那棵树里而不是宿主 `~`。布局只写在这一处，
    调用方不该自己拼 ".claude"/"projects"。
    """
    return (home or Path.home()) / ".claude" / "projects"


def find_transcript(session_id: str, root: Path | None = None) -> Path | None:
    base = root or projects_root()
    if not base.exists():
        return None
    for candidate in base.glob(f"*/{session_id}.jsonl"):
        return candidate
    return None


def parse_tool_calls(path: Path) -> tuple[ToolCall, ...]:
    """重建工具调用序列。

    assistant 记录里的 tool_use 块给出调用，紧随其后的 user 记录里的
    tool_result 块（按 tool_use_id 关联）给出成败。
    """
    if not path or not Path(path).exists():
        return ()

    calls: list[ToolCall] = []
    errors: dict[str, bool] = {}

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        name=block.get("name", "unknown"),
                        call_id=block.get("id", ""),
                    )
                )
            elif block.get("type") == "tool_result":
                errors[block.get("tool_use_id", "")] = bool(
                    block.get("is_error", False)
                )

    return tuple(
        ToolCall(
            name=c.name, call_id=c.call_id, is_error=errors.get(c.call_id, False)
        )
        for c in calls
    )
