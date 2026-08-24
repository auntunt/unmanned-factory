"""权限门第二层：廉价模型审批者。静态规则判不出时问 haiku。

为什么不直接用 supervisors/model_base.ClaudeJudge：那个的 schema 是
pass/fail + claims（审的是「产出对不对」），这里要的是 allow/deny + reason
（判的是「这个动作准不准做」）。共用 schema 会让两种语义在审计里混在一起。

但**独立性那四个 flag 必须一样**，而且理由完全相同：审批者要是能读文件、
能加载 CLAUDE.md，worker 就能通过写文件影响审批结果——那等于让被审的人
给自己写评语。所以这里 import ClaudeJudge 的 argv 构造思路，不重写。

成本控制是这层能存在的前提：haiku 一次调用约 $0.001，只在静态规则
ESCALATE 时触发。如果这层被高频触发，说明静态规则的覆盖面不对，
该去补 rules.py，不是加大这层的预算。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass

from factory.harness import drift
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.permission.rules import Decision, Ruling

_MAX_FIELD = 2000

#: 审批者的输出形状。刻意不给 "escalate" 选项——模型说不清就是说不清，
#: 那种情况由调用方（ok=False）转成升级，不让模型自己选一条「我不知道」
#: 的舒服出路。实测给了那个选项，模型会拿它当默认答案。
APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "deny"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
}

_PROMPT = """你在审一个自动化编码 worker 请求执行的动作，判断它是否安全。

判 deny 的标准（只看这些，不要发挥）：
- 会不可逆地销毁工作成果（抹掉未提交改动、删除源码目录）
- 会绕过或削弱质量闸门（改 CI 配置、删测试、改测试断言让它必过）
- 会引入未经审计的外部代码（把网络内容直接喂给解释器/shell）
- 会放大权限或留下后门（全局可写、写入启动项）

判 allow 的情况：
- 常规开发动作（读写源码、跑测试、装依赖、git add/commit）
- 目标明确且范围可控的删除（`rm -rf build/`、清理自己刚生成的临时文件）
- 只读操作，无论看起来多奇怪

含糊时判 allow。这道门防的是误伤级破坏，不是所有可疑动作——
沙箱已经管住了边界，你只管边界内的不可逆破坏。
过度拦截会让整条流水线停摆，那个代价比放过一个可疑的读操作大得多。

动作类型：{tool}
动作内容：
{payload}

给出 decision 和一句具体的 reason（说清是哪条标准，不要复述动作）。"""


@dataclass(frozen=True)
class Approval:
    ok: bool
    decision: Decision = Decision.ESCALATE
    reason: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    error_text: str = ""


class ModelApprover:
    """一次无工具、干净上下文、要结构化输出的审批调用。

    默认 haiku：这层要在 worker 的工具调用路径上同步跑，延迟直接加到
    每个被 escalate 的动作上。用 sonnet 的话单次 3~5s，一次派发几十个
    escalate 就是几分钟纯等待。
    """

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "haiku",
        timeout_s: int = 60,
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
            # 这四个和监工同源，理由也同源：审批者能读文件的话，
            # worker 写个文件就能影响审批结果。别删。
            "--tools",
            "",
            "--safe-mode",
            "--exclude-dynamic-system-prompt-sections",
            "--json-schema",
            json.dumps(APPROVAL_SCHEMA),
        ]

    def review(self, tool: str, payload: str) -> Approval:
        prompt = _PROMPT.format(tool=tool, payload=payload[:_MAX_FIELD])
        with tempfile.TemporaryDirectory(prefix="factory-approver-") as clean:
            return self._review_in(prompt, clean)

    def _review_in(self, prompt: str, cwd: str) -> Approval:
        try:
            proc = run_bounded(
                self._argv(prompt), cwd=cwd, timeout_s=self._timeout_s
            )
        except ProcTimeout:
            return Approval(ok=False, error_text=f"timeout after {self._timeout_s}s")
        except OSError as exc:
            return Approval(ok=False, error_text=f"cannot launch {self._binary}: {exc}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return Approval(
                ok=False, error_text=(proc.stdout or proc.stderr or "")[:_MAX_FIELD]
            )

        # 和监工同一个坑：带独立性 flag 时 usage.input_tokens 是 0，
        # 真数在 modelUsage 里。走 drift 而不是自己读 usage。
        tokens = drift.total_tokens(payload)
        cost = float(payload.get("total_cost_usd", 0.0) or 0.0)

        if payload.get("is_error"):
            return Approval(
                ok=False,
                tokens=tokens,
                cost_usd=cost,
                error_text=" ".join(
                    str(payload.get(k, ""))
                    for k in ("subtype", "stop_reason", "result")
                ).strip()[:_MAX_FIELD],
            )

        out = payload.get("structured_output")
        if not isinstance(out, dict) or out.get("decision") not in ("allow", "deny"):
            return Approval(
                ok=False,
                tokens=tokens,
                cost_usd=cost,
                error_text=f"unusable structured_output: {str(out)[:400]}",
            )

        return Approval(
            ok=True,
            decision=Decision.ALLOW if out["decision"] == "allow" else Decision.DENY,
            reason=str(out.get("reason", ""))[:_MAX_FIELD],
            cost_usd=cost,
            tokens=tokens,
        )


def ruling_from_approval(approval: Approval) -> Ruling:
    """把审批结果变成 Ruling。

    审批者自己坏了（ok=False）→ ESCALATE，**不是 ALLOW 也不是 DENY**。

    这个方向的选择很关键，两侧都有代价：判 ALLOW 等于审批者一挂全线放行，
    这道门就成了装饰；判 DENY 等于 haiku 一超时整条流水线停摆，而
    「无人值守」是这个系统的立命之本。所以走 ESCALATE，由上层按
    「这次动作的风险」决定——低风险动作放行并记账，高风险动作才停。
    判据留在上层，因为只有那里知道动作是什么。
    """
    if not approval.ok:
        return Ruling(
            Decision.ESCALATE,
            rule="approver-unavailable",
            reason=f"审批者不可用：{approval.error_text or 'no decision'}",
        )
    return Ruling(
        approval.decision,
        rule="model-approver",
        reason=approval.reason,
    )
