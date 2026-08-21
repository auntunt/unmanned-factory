"""PreToolUse hook 客户端。**只用标准库，绝不 import factory.\\***。

这是权限门唯一能真正拦住动作的接入点。实测（claude CLI 2.x）：

    $ claude -p "Run the bash command: echo HELLO" --settings <带 PreToolUse 的 json>
    → 结果 JSON 里出现 permission_denials:[{tool_name:"Bash",...}]
      result: "The command was blocked by a hook with the message: `...`"

transcript 解析做不到这件事：那是**事后**的记录，命令早就跑完了。

为什么不能 import factory：沙箱后端 (sandbox_linux) 第 2 段有
`--tmpfs <factory_root>`，工厂代码在 worker 眼里整个不存在。这个脚本由
broker 复制到一个 bind-mount 进沙箱的目录里，靠 socket 跟外面的门通话。
一旦这里 `from factory.permission import ...`，沙箱模式下必然 ImportError,
而 hook 失败的表现是**静默放行**——也就是没人会来报的那一侧。

失败一律放行（fail-open），和 rules.py 的默认 ALLOW 同一个理由：这道门防的是
误伤级破坏，不是所有可疑动作；边界防护是沙箱。broker 挂了就让任务照常跑并在
审计里留痕，比让整条无人值守流水线停摆划算。要 fail-closed 的话，一个 socket
抖动就能让所有任务全灭。
"""

from __future__ import annotations

import json
import os
import socket
import sys

#: broker socket 路径。由 claude_code.py 通过子进程 env 传进来。
SOCKET_ENV = "FACTORY_GATE_SOCKET"

#: 等 broker 回话的上限。要盖住模型审批者的整个 review（默认 timeout_s=60），
#: 否则 escalate 的动作会在这里超时 → 放行，第 2 层等于没接。留 30s 余量。
_TIMEOUT_S = 90.0

_ALLOW_SILENT = ""


def decide(payload: dict, sock_path: str | None) -> str:
    """问 broker 要一个判决，返回要打到 stdout 的字符串（空串 = 放行）。"""
    if not sock_path:
        return _ALLOW_SILENT

    req = json.dumps(
        {
            "tool": payload.get("tool_name", ""),
            "args": payload.get("tool_input") or {},
        }
    )

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_TIMEOUT_S)
            s.connect(sock_path)
            s.sendall(req.encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.decode("utf-8").strip() or "{}")
    except (OSError, json.JSONDecodeError):
        # broker 不在 / 超时 / 回了垃圾 —— 放行。见模块 docstring。
        return _ALLOW_SILENT

    if reply.get("decision") != "deny":
        # allow 和 escalate 都走这条：**不输出 allow 判决**。
        # 显式回 permissionDecision:"allow" 会跳过 CLI 自己后续的权限检查，
        # 等于我们这道补充的门把主防线关掉了。空输出 = 交回正常流程。
        return _ALLOW_SILENT

    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": str(reply.get("reason", ""))[:2000],
            }
        }
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    out = decide(payload, os.environ.get(SOCKET_ENV))
    if out:
        sys.stdout.write(out)
    # 退出码永远 0：非 0 会被 CLI 当成 hook 自身故障，行为随版本而变。
    # 判决只通过 stdout 的 JSON 表达，这条路径实测过。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
