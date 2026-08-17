"""带真实超时的子进程执行。**超时要杀掉整棵树，不只是那个直接子进程。**

为什么不用 `subprocess.run(timeout=...)`：它在超时后只 kill 直接子进程。
真实的 claude CLI 是个 node 进程，会再拉起 MCP server、ripgrep、以及 Bash
工具跑的每一条命令。父进程被 kill 之后这些全部继续跑 —— 而它们才是花钱的
那一半。

实测（2026-08-06，一个 `sleep 300` 的假 worker，`--timeout 5`）：
一次 drain 跑完，`pgrep -f 'sleep 300'` 还剩 **12 个孤儿**。循环早已停机、
报表早已打印，那 12 个进程还在跑。换成真 CLI，这就是「停机之后还在烧钱」，
而且烧的是我们已经承认记不上账的那种钱 —— 熔断器停了机，孤儿照样烧。

所以这里改用 Popen + `start_new_session=True`：子进程自己成一个进程组，
超时时 `killpg` 整组。两阶段（先 TERM 再 KILL）不是客气，是为了让 CLI 有
机会把 transcript 落盘 —— 漏账那条警告唯一能给人的线索就是 transcript
路径，杀得太干净等于把唯一的证据也删了。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

# TERM 之后等多久再 KILL。够 node 跑完 flush，又不至于让「超时」变成
# 「超时 + 一段谁也说不清的额外等待」。
GRACE_S = 3.0


@dataclass(frozen=True)
class Completed:
    """subprocess.CompletedProcess 的子集。只留 harness 真的会读的三个字段。"""

    returncode: int
    stdout: str
    stderr: str


class Timeout(Exception):
    """超时并且**已经把整组杀掉了**。

    刻意不复用 subprocess.TimeoutExpired：那个异常的语义是「等超时了」，
    不保证进程死了。两个 harness 的 except 分支要能一眼看出树已经清掉。

    hard_killed = TERM 之后 GRACE_S 内没退，得上 KILL。它进审计的 error_text：
    「超时」和「超时且拒绝优雅退出」是两种不同的现场 —— 后者意味着 transcript
    可能没来得及落盘，而漏账警告唯一能给人的线索就是那个文件。
    """

    def __init__(self, timeout_s: float, hard_killed: bool) -> None:
        super().__init__(f"timeout after {timeout_s}s"
                         + ("（TERM 无效，已 KILL 整个进程组）"
                            if hard_killed else ""))
        self.timeout_s = timeout_s
        self.hard_killed = hard_killed


def _killpg(pgid: int, sig: int) -> bool:
    """给整组发信号。组已经没了返回 False —— 那是正常竞态，不是错误。"""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def run_bounded(argv: list[str] | str, *, cwd: Path | str | None = None,
                env: dict[str, str] | None = None,
                timeout_s: float | None = None,
                shell: bool = False) -> Completed:
    """跑一个程序，超时杀整棵进程树，抛 Timeout。

    timeout_s=None 表示不限时（探针路径不需要这一层）。

    shell=True 是给回归监工用的：task YAML 里的 check 命令本来就是 shell
    串（`pytest -q && npm test`）。这条路**更**需要杀整组 —— sh 自己派生的
    东西 sh 都不管，而 check 命令是用户写的，我们对它派生什么没有任何假设。
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, text=True, shell=shell,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # 这一行是整个模块存在的理由。没有它，下面的 killpg 会打到
        # **我们自己**的进程组 —— 那等于工厂自杀。
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise Timeout(timeout_s or 0.0, _reap(proc)) from None
    return Completed(proc.returncode, out or "", err or "")


def _reap(proc: subprocess.Popen) -> bool:
    """TERM 整组 → 等 GRACE_S → 还活着就 KILL 整组 → 把管道抽干。

    返回「是否动用了 KILL」。

    最后那步不能省：不 communicate 的话父进程留着两个还开着的管道和一个
    僵尸，而无人循环要连着跑几百个任务，攒够了就是 EMFILE。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = 0
    if pgid:
        _killpg(pgid, signal.SIGTERM)
    hard = False
    try:
        proc.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        # 直接子进程还没死。它的后代死没死这里看不到（也无法便宜地看到），
        # 所以 KILL 照发 —— 组信号对已经死掉的组是个 no-op，代价为零。
        hard = True
    if pgid:
        _killpg(pgid, signal.SIGKILL)
    # 抽干管道、回收僵尸。
    #
    # 这里的 communicate 会超时，而且不算罕见：只要被杀的进程派生过**逃出进程组**
    # 的后代（`os.setsid()` 之后 sleep，或者 nohup/setsid 起的守护进程），killpg
    # 就打不到那个后代，它继承的管道写端一直开着，communicate 永远读不到 EOF。
    # 实测 10/10 次触发（/tmp/verify_c2_real.py：子进程 fork 后 setsid 再 sleep）。
    #
    # 关键是**不能让 subprocess.TimeoutExpired 穿出去**。run_bounded 的契约是
    # 超时抛 factory 自己的 Timeout；漏一个 TimeoutExpired 出去，上层会当成
    # 未预期崩溃处理，任务被判为 harness 故障而不是超时。曾经写成再 communicate
    # 一次而不 catch，就是这个后果。
    #
    # 至于 fd：Popen 的管道由 Popen 对象持有，函数返回后引用归零就关掉了，
    # 不 catch 也不泄漏（实测连跑 10 次 delta=0）。所以这里 catch 之后
    # 什么都不用做 —— 逃出去的孙子进程我们本来就管不了，那是它自己的生命周期。
    try:
        proc.communicate(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        # 逃出进程组的后代还抱着管道写端。已经 KILL 过整组，能做的都做了。
        # 显式关掉自己这侧的管道，不依赖 GC 的时机。
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
    return hard
