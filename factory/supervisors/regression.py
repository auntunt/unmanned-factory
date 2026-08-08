"""回归监工：跑命令、比对输出。确定性，零模型调用。

claims 是打回 worker 时的唯一载荷，所以每条都必须写清
「哪个 check / 期望什么 / 实得什么」—— 含糊的 claim 会让重试轮次白烧。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from factory.audit.models import SupervisorRole, Verdict
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.supervisors.base import SupervisorReport
from factory.task import CheckSpec

_MAX_CAPTURE = 2000


def _sh(command: str, workspace: Path, timeout_s: int):
    # 不是 subprocess.run：这里 shell=True，command 是 task YAML 里的一串
    # 用户写的 shell。超时只 kill 那个 sh，它拉起的 pytest / node / docker
    # 全都活下来 —— 一条挂住的 check 能在机器上留一地进程。见 proc 模块。
    return run_bounded(
        command,
        cwd=workspace,
        timeout_s=timeout_s,
        shell=True,
    )


def run_check(spec: CheckSpec, workspace: Path) -> dict | None:
    """跑一条 check。通过返回 None，不通过返回一条 claim。"""
    try:
        proc = _sh(spec.command, workspace, spec.timeout_s)
    except ProcTimeout:
        # 这条 got 文本是承重的：漏账探测器靠 "timeout" in got.lower() 认
        # 超时（factory/backlog 的 is_untracked_spend）。别改措辞。
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": spec.expect,
            "got": f"timeout after {spec.timeout_s}s",
        }

    if spec.expect == "exit_zero":
        if proc.returncode == 0:
            return None
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": "exit_zero",
            "got": (
                f"exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:_MAX_CAPTURE]}"
            ),
        }

    if spec.expect == "stdout_contains":
        if spec.value in proc.stdout:
            return None
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": f"stdout contains {spec.value!r}",
            "got": proc.stdout[:_MAX_CAPTURE],
        }

    if spec.expect == "commands_agree":
        try:
            other = _sh(spec.value, workspace, spec.timeout_s)
        except ProcTimeout:
            return {
                "check": spec.name,
                "command": spec.value,
                "expected": "commands_agree",
                "got": f"timeout after {spec.timeout_s}s",
            }
        if proc.stdout.strip() == other.stdout.strip():
            return None
        return {
            "check": spec.name,
            "command": f"{spec.command} vs {spec.value}",
            "expected": other.stdout.strip()[:_MAX_CAPTURE],
            "got": proc.stdout.strip()[:_MAX_CAPTURE],
        }

    return {
        "check": spec.name,
        "command": spec.command,
        "expected": spec.expect,
        "got": f"unknown expect {spec.expect!r}",
    }


class RegressionSupervisor:
    role = SupervisorRole.REGRESSION

    def review(
        self, workspace: Path, checks: Sequence[CheckSpec]
    ) -> SupervisorReport:
        if not checks:
            return SupervisorReport(
                role=self.role,
                verdict=Verdict.FAIL,
                claims=(
                    {
                        "check": "no-checks-defined",
                        "command": "",
                        "expected": "至少一条廉价客观裁判",
                        "got": "任务没有定义任何 check，不能判为通过",
                    },
                ),
            )

        claims = [
            c
            for c in (run_check(s, Path(workspace)) for s in checks)
            if c is not None
        ]
        return SupervisorReport(
            role=self.role,
            verdict=Verdict.FAIL if claims else Verdict.PASS,
            claims=tuple(claims),
        )
