"""回归监工：跑命令、比对输出。确定性，零模型调用。

claims 是打回 worker 时的唯一载荷，所以每条都必须写清
「哪个 check / 期望什么 / 实得什么」—— 含糊的 claim 会让重试轮次白烧。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from factory.audit.models import SupervisorRole, Verdict
from factory.harness.checkenv import check_env
from factory.harness.proc import Timeout as ProcTimeout
from factory.harness.proc import run_bounded
from factory.supervisors.base import SupervisorReport
from factory.task import CheckSpec

_MAX_CAPTURE = 2000
_TAIL_BUDGET = 4000  # pytest 失败摘要在尾部，至少保留这么多


def _format_output(stdout: str, stderr: str, tail_only: bool = False) -> str:
    """格式化 stdout/stderr，同时捕获两路，优先保留尾部。

    tail_only=True 时只保留尾部 _TAIL_BUDGET 字符（pytest 失败摘要在末尾）。
    tail_only=False 时保留头部 _MAX_CAPTURE 字符（向后兼容短输出场景）。

    stderr 只有噪声警告、stdout 才是失败摘要的情况下，不能因为 stderr 非空
    就丢掉 stdout —— 这就是 regression claim 里诊断消失的根因。
    """
    parts = []
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if stdout:
        parts.append(f"[stdout]\n{stdout}")

    combined = "\n".join(parts) if parts else ""

    if not combined:
        return ""

    if tail_only:
        # 优先保留尾部：pytest 的 FAILED / assert 摘要在末尾
        if len(combined) > _TAIL_BUDGET:
            return "...\n" + combined[-_TAIL_BUDGET:]
        return combined
    else:
        # 向后兼容：短输出场景保留头部
        if len(combined) > _MAX_CAPTURE:
            return combined[:_MAX_CAPTURE] + "\n..."
        return combined


def _sh(command: str, workspace: Path, timeout_s: int):
    # 不是 subprocess.run：这里 shell=True，command 是 task YAML 里的一串
    # 用户写的 shell。超时只 kill 那个 sh，它拉起的 pytest / node / docker
    # 全都活下来 —— 一条挂住的 check 能在机器上留一地进程。见 proc 模块。
    #
    # env=check_env()：白名单式最小环境，不继承工厂进程的凭据（H-3）。
    # check 命令来自 task YAML，可能是模型生成的，权限不该等于工厂本身。
    return run_bounded(
        command,
        cwd=workspace,
        timeout_s=timeout_s,
        shell=True,
        env=check_env(),
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
            "got": f"exit {proc.returncode}:\n{_format_output(proc.stdout, proc.stderr, tail_only=True)}",
        }

    if spec.expect == "stdout_contains":
        if spec.value in proc.stdout:
            return None
        return {
            "check": spec.name,
            "command": spec.command,
            "expected": f"stdout contains {spec.value!r}",
            "got": _format_output(proc.stdout, proc.stderr, tail_only=True),
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
            "expected": _format_output(other.stdout.strip(), other.stderr.strip(), tail_only=True),
            "got": _format_output(proc.stdout.strip(), proc.stderr.strip(), tail_only=True),
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
