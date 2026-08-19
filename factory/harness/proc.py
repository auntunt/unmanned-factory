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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# TERM 之后等多久再 KILL。够 node 跑完 flush，又不至于让「超时」变成
# 「超时 + 一段谁也说不清的额外等待」。
GRACE_S = 3.0

# 停滞检测的轮询间隔。0.2s 足够及时（停滞阈值是分钟级），又不至于让
# 一个空转的 while 占掉可观的 CPU —— 无人循环会连着跑几百个任务。
_POLL_S = 0.2


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

    stalled = 不是跑满 timeout_s，而是「活着但一直没有任何输出」被提前杀掉。
    两者进审计要能分辨：跑满超时说明任务太大或判据太慢，是**任务**的问题；
    停滞说明 worker CLI 起来了却没发出请求（实测 CPU 0%、ep_poll、无网络连接），
    是**环境**的问题。混成一种，就会拿模型阶梯去重试一个环境故障。
    """

    def __init__(self, timeout_s: float, hard_killed: bool,
                 stalled: bool = False, idle_s: float | None = None) -> None:
        if stalled:
            msg = (f"stalled: no output for {idle_s:.0f}s "
                   f"(limit {timeout_s}s not reached)")
        else:
            msg = f"timeout after {timeout_s}s"
        super().__init__(msg + ("（TERM 无效，已 KILL 整个进程组）"
                                if hard_killed else ""))
        self.timeout_s = timeout_s
        self.hard_killed = hard_killed
        self.stalled = stalled
        self.idle_s = idle_s


def _killpg(pgid: int, sig: int) -> bool:
    """给整组发信号。组已经没了返回 False —— 那是正常竞态，不是错误。"""
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _drain(stream, chunks: list[str], counter: list[int]) -> None:
    """把一条管道抽干到 EOF，边抽边记字节数。

    按行读而不是 read()：read() 要等 EOF 才返回，那就又变成一次性阻塞，
    观察不到增量。counter 用单元素 list 而不是 int —— 主线程要看到它变，
    而 int 是不可变的。GIL 保证 += 对 list 元素的赋值不会撕裂。
    """
    try:
        for line in iter(stream.readline, ""):
            chunks.append(line)
            counter[0] += len(line)
    except (OSError, ValueError):
        # 管道被 _reap 关掉了。正常竞态，不是错误。
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def run_bounded(argv: list[str] | str, *, cwd: Path | str | None = None,
                env: dict[str, str] | None = None,
                timeout_s: float | None = None,
                shell: bool = False,
                stall_timeout_s: float | None = None) -> Completed:
    """跑一个程序，超时杀整棵进程树，抛 Timeout。

    timeout_s=None 表示不限时（探针路径不需要这一层）。

    stall_timeout_s 是**停滞**上限：进程活着但连续这么久没有任何 stdout/stderra
    增量，就判定挂死，提前杀掉并抛 Timeout(stalled=True)。None 表示不检测。

    为什么需要它：实测 claude CLI 会随机挂死 —— 进程起来了，CPU 0%、卡在
    ep_poll、**没有任何到中转站的网络连接**，也不退出。光靠 timeout_s 得干等
    满 15 分钟，而且那种 attempt 记 $0，预算闸门拦不住。一次真实派发里三轮
    有两轮这么烧掉了。

    判据必须是「无输出增量」而不是「总时长」：真在生成的慢任务一直有 token
    流出，不能被误杀；挂死的进程一个字节都没有。

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

    # 不开停滞检测就走原路。communicate 比双线程便宜，而且探针/短命令
    # （version 探测那种）没必要为了一个用不上的功能多两个线程。
    if stall_timeout_s is None:
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise Timeout(timeout_s or 0.0, _reap(proc)) from None
        return Completed(proc.returncode, out or "", err or "")

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    seen = [0]  # 两条管道共用一个计数器：任一条有输出就算「还活着」
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_chunks, seen),
                         daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_chunks, seen),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    start = time.monotonic()
    last_seen, last_change = 0, start
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if timeout_s is not None and now - start >= timeout_s:
            raise Timeout(timeout_s, _reap(proc)) from None
        if seen[0] != last_seen:
            last_seen, last_change = seen[0], now
        elif now - last_change >= stall_timeout_s:
            # 活着但一直没吐字节。杀掉并标 stalled —— 上层据此区分
            # 「任务太慢」和「worker 挂死」，后者不该走模型阶梯重试。
            raise Timeout(timeout_s or 0.0, _reap(proc),
                          stalled=True, idle_s=now - last_change) from None
        time.sleep(_POLL_S)

    # 进程已退出，但管道里可能还有缓冲没读完。join 有上限：逃出进程组的
    # 后代仍可能抱着写端不放（见 _reap 的注释），那时 readline 永远等不到
    # EOF，无限 join 会把整个循环挂死在这里 —— 比原来的 bug 更糟。
    for t in threads:
        t.join(timeout=GRACE_S)
    return Completed(proc.returncode, "".join(out_chunks), "".join(err_chunks))


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
    except OSError:
        # 走停滞检测那条路时，_drain 线程已经把 stdout/stderr close() 了，
        # 这里的 communicate 再去读就是 EBADF（[Errno 9] Bad file descriptor）。
        #
        # 必须在这里咽掉：_reap 是**清理**函数，它的职责是回收，不是报错。
        # 漏一个 OSError 出去，会被 claude_code / shell 的 `except OSError`
        # 当成「进程压根没启动」，claim 里写成 `cannot launch claude: ...`
        # —— 而真相是进程跑过了、被我们自己杀的。实测这个假象让三轮
        # haiku/sonnet/opus 全烧在一个错误方向上。
        pass
    return hard
