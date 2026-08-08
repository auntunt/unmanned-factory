"""调模型的监工底座。spec §4.1：独立性来自**扣掉的输入**和干净上下文，
不来自提示词里写一句「请独立判断」。

三道结构性约束，都可测：
  1. `--tools ""`  监工没有任何工具，够不到没递给它的东西。实测过：它会把
     tool_call 当纯文本吐出来，然后什么也读不到 —— 所以「不给 build log」
     不会被它自己打开文件绕过。
  2. `--safe-mode` 不加载 CLAUDE.md / skills / hooks / MCP / 插件。§4.1 里
     架构监工那句「干净上下文即独立性」就是这一条。
  3. `_assert_no_leak` 扣掉的输入若出现在最终 prompt 里，直接抛异常。写成
     运行时断言而非只写测试：以后改 prompt 组装的人没读过 §4.1 也会被拦下。

调用失败判 FAIL 不判 PASS —— 监工挂了却放行，正是 §4.1 要防的盖章通过。
这类 claim 的 check 带 `supervisor-` 前缀，好和「真发现问题」在报表里分开，
dispatcher 也不会把它当修复指令发给 worker（worker 修不了监工的故障）。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass

from factory.audit.models import SupervisorRole, Verdict
from factory.harness import drift
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.supervisors.base import SupervisorReport

SUPERVISOR_ERROR_PREFIX = "supervisor-"

_CLAIM_KEYS = ("check", "command", "expected", "got")
_MAX_FIELD = 2000

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "expected": {"type": "string"},
                    "got": {"type": "string"},
                },
                "required": ["check", "expected", "got"],
            },
        },
    },
    "required": ["verdict", "claims"],
}


class LeakError(RuntimeError):
    """扣掉的输入漏进了 prompt。是 bug，不是可恢复错误，所以抛而不是判 FAIL。"""


@dataclass(frozen=True)
class ModelCall:
    ok: bool
    verdict: str = "fail"
    claims: tuple[dict, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0
    error_text: str = ""


def coerce_claims(raw) -> tuple[dict, ...]:
    """模型给的 claim 只留已知字段、全部转字符串。

    claims 会原样进审计库、也会原样打回 worker，所以宁可丢掉多余字段，
    也不让形状不定的东西流进下游。
    """
    if not isinstance(raw, list):
        return ()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        claim = {
            k: str(item.get(k, ""))[:_MAX_FIELD]
            for k in _CLAIM_KEYS
            if item.get(k) not in (None, "")
        }
        if claim.get("check"):
            out.append(claim)
    return tuple(out)


def assert_no_leak(prompt: str, withheld: tuple[str, ...]) -> None:
    for item in withheld:
        text = (item or "").strip()
        # 太短的片段会假报（比如空 build log），只查有辨识度的长度
        if len(text) >= 24 and text in prompt:
            raise LeakError(
                f"扣掉的输入漏进了 prompt（前 60 字：{text[:60]!r}）—— "
                "spec §4.1 的独立性靠的就是这个输入没被看到"
            )


class ClaudeJudge:
    """一次无工具、无自定义上下文、要结构化输出的模型调用。

    退出码不可信这条和 harness adapter 同源（Global Constraints）：只读 is_error。

    `cwd` 必须是空目录，这一条是实测出来的，不是保险起见：
    `claude -p` 会把当前工作目录和 `git status` 的文件清单塞进系统提示词。
    第一次真跑时监工就因此拿着**编排层自己的**仓库约定去评审目标仓库，
    报出了「本仓库源码统一放在 factory/ 包下」这种张冠李戴的意见。
    所以 cwd 指向一个空临时目录，再加 --exclude-dynamic-system-prompt-sections
    掐掉动态注入 —— 干净上下文（spec §4.1）得同时管住这两个来源。
    """

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "sonnet",
        timeout_s: int = 300,
    ) -> None:
        self._binary = binary
        self._model = model
        self._timeout_s = timeout_s

    def _argv(self, prompt: str) -> list[str]:
        return [
            self._binary,
            "-p",
            prompt,
            "--model",
            self._model,
            "--output-format",
            "json",
            # 这四个 flag 就是监工的独立性，不在提示词措辞里。别删。
            "--tools",
            "",
            "--safe-mode",
            "--exclude-dynamic-system-prompt-sections",
            "--json-schema",
            json.dumps(VERDICT_SCHEMA),
        ]

    def ask(self, prompt: str) -> ModelCall:
        with tempfile.TemporaryDirectory(prefix="factory-judge-") as clean:
            return self._ask_in(prompt, clean)

    def _ask_in(self, prompt: str, cwd: str) -> ModelCall:
        try:
            # 不是 subprocess.run：三个模型监工都走这条路，每次超时都会留下
            # 一棵还在花钱的 node 进程树。见 proc 模块。
            proc = run_bounded(
                self._argv(prompt),
                cwd=cwd,
                timeout_s=self._timeout_s,
            )
        except ProcTimeout:
            return ModelCall(ok=False, error_text=f"timeout after {self._timeout_s}s")
        except OSError as exc:
            return ModelCall(ok=False, error_text=f"cannot launch {self._binary}: {exc}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ModelCall(
                ok=False, error_text=(proc.stdout or proc.stderr or "")[:_MAX_FIELD]
            )

        # 走 drift.total_tokens 而不是自己读 usage：监工带上四个独立性 flag 时
        # `usage.input_tokens` 实测是 **0**（真数在 modelUsage 里）。原来这行
        # 读 usage，于是所有模型监工的 token 都记 0 —— 真跑复现过。
        tokens = drift.total_tokens(payload)
        cost = float(payload.get("total_cost_usd", 0.0) or 0.0)

        # 格式漂移。走 ok=False 而不是加个标记继续：那条路已经会判 FAIL 且
        # 带 SUPERVISOR_ERROR_PREFIX（升级给人，不打回 worker），正是漂移
        # 该去的地方 —— 读不懂输出是我们的故障，不是 worker 的错。
        #
        # 监工这一路比 worker 那一路更值得加固：P1 真跑单任务 $0.65 **全部**
        # 落在两个模型监工上，而监工的花费记在 verdict 行、attempt 行照常收费，
        # 所以 `is_untracked_spend` 第一行 `if row.cost_usd: return False`
        # 会直接放过它。实测：attempt $0.12 + 监工漏账，账上 $0.12，熔断器 False。
        if (missing := drift.missing_fields(payload)):
            return ModelCall(ok=False, tokens=tokens, cost_usd=cost,
                             error_text=drift.drift_text(missing)[:_MAX_FIELD])

        if payload.get("is_error"):
            return ModelCall(
                ok=False,
                tokens=tokens,
                cost_usd=cost,
                error_text=" ".join(
                    str(payload.get(k, ""))
                    for k in ("subtype", "stop_reason", "result")
                ).strip()[:_MAX_FIELD],
            )

        out = payload.get("structured_output")
        if not isinstance(out, dict) or out.get("verdict") not in ("pass", "fail"):
            # 拿不到可解析的裁决就是没裁决。不猜、不当 pass。
            return ModelCall(
                ok=False,
                tokens=tokens,
                cost_usd=cost,
                error_text=f"unusable structured_output: {str(out)[:400]}",
            )

        return ModelCall(
            ok=True,
            verdict=out["verdict"],
            claims=coerce_claims(out.get("claims")),
            tokens=tokens,
            cost_usd=cost,
        )


def report_from_call(
    role: SupervisorRole, call: ModelCall, *, what: str
) -> SupervisorReport:
    """把一次模型调用变成裁决。调用失败 → FAIL，且 claim 打上前缀。"""
    if not call.ok:
        return SupervisorReport(
            role=role,
            verdict=Verdict.FAIL,
            claims=(
                {
                    "check": f"{SUPERVISOR_ERROR_PREFIX}{role.value}-unavailable",
                    "command": "",
                    "expected": f"{what}给出 pass/fail 裁决",
                    "got": call.error_text or "no verdict",
                },
            ),
            tokens=call.tokens,
            cost_usd=call.cost_usd,
        )

    verdict = Verdict.PASS if call.verdict == "pass" else Verdict.FAIL
    claims = call.claims
    if verdict == Verdict.FAIL and not claims:
        # 判 fail 却不说哪儿错，等于不可执行的打回。当监工故障处理。
        claims = (
            {
                "check": f"{SUPERVISOR_ERROR_PREFIX}{role.value}-empty-claims",
                "command": "",
                "expected": "fail 必须附具体失败项",
                "got": "模型判 fail 但 claims 为空",
            },
        )
    return SupervisorReport(
        role=role,
        verdict=verdict,
        claims=() if verdict == Verdict.PASS else claims,
        tokens=call.tokens,
        cost_usd=call.cost_usd,
    )
