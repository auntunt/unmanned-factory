"""进程围栏：给 worker 上 RLIMIT_NPROC，防失控 fork 拖垮整机。

抄 handoff 的 proc_fence 段。它的理由值得原样记下来：agentd 由 systemd 拉起，
worker 失控 fork 时整机进程表打满，**连 sshd 都起不来**——那时你既看不到
现场也进不去机器。所以留一条"救护车道"：按当前余量算上限，给系统留
reserve_ratio 的空间。

为什么不用 cgroup：cgroup v2 的 pids.max 更准，但要 root 或 delegate 过的
子树。RLIMIT_NPROC 是 setrlimit(2)，非特权进程能给自己降（只能降不能升），
用 preexec_fn 在 fork 后 exec 前设上，零权限要求。

**RLIMIT_NPROC 是按 uid 计的，不是按进程树。** 意味着上限里也算着编排层
自己的进程。所以 headroom 要按"当前这个 uid 已经用了多少"算，而不是拿
系统总量除一除——后者在多用户机器上会算出一个根本拦不住的数。

失效模式是刻意的软失败：算不出余量、或 setrlimit 被拒时不阻止派发，
只在 fence_note 里留痕。围栏是纵深防御的第二层（第一层是沙箱），
不该因为它不可用就让整条流水线停摆。
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
from dataclasses import dataclass

#: 给系统留的救护车道比例。0.1 = 余量的 10% 不给 worker 用。
#: handoff 用的也是 0.1，没有更好的理由，就抄了。
DEFAULT_RESERVE_RATIO = 0.1

#: 围栏下限。低于这个数 worker 连自己的工具链都起不来
#: （claude 是 node，要 fork 出 npm/git/pytest 若干），围栏就没有意义了。
#: 算出来比这还小时判定"机器本来就快满了"，不设围栏而是报告出去。
MIN_VIABLE_LIMIT = 64


@dataclass(frozen=True)
class Fence:
    """一次围栏决定。limit 为 None 表示不设。"""

    limit: int | None
    note: str

    @property
    def enabled(self) -> bool:
        return self.limit is not None


def current_usage(uid: int | None = None) -> int | None:
    """当前 uid 已用的**内核调度实体**数（进程 + 线程）。数不出来返回 None。

    **必须数线程，不能只数进程。** Linux 的 RLIMIT_NPROC 限制的是 task
    数量，一个 40 线程的 JVM 占 40 个配额而不是 1 个。实测本机 38 个进程
    对应 245 个 task —— 差 6.4 倍。

    按进程数算的后果实测过一次：used 报 38、hard 14313，算出围栏 78，
    结果子进程**第一个 fork 就 EAGAIN**（真实占用 245 已经超过 78）。
    生产上 hard 够大时看不出来，但机器负载高或 ulimit -u 被调小时，
    这个低估会直接算出一个把 worker 闷死的围栏，而症状是 worker 起不来、
    跟"围栏"毫无字面关联。

    /proc 扫一遍比 `ps -eLf` 便宜且不依赖外部命令。读的过程中进程退出
    是常态，跳过即可——要的是量级，不是精确快照。
    """
    want = os.getuid() if uid is None else uid
    if sys.platform == "darwin":
        # Darwin documents RLIMIT_NPROC as simultaneous *processes* for a uid,
        # unlike Linux's task accounting.  There is no /proc task tree here;
        # ask the native ps for its uid/pid process table rather than pretending
        # that a Linux thread count exists.  The command is fixed argv, bounded,
        # and failure remains the documented soft failure.
        try:
            listing = subprocess.run(
                ["ps", "-axo", "uid=,pid="], text=True, capture_output=True,
                timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if listing.returncode:
            return None
        count = 0
        for line in listing.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                process_uid = int(fields[0])
                int(fields[1])
            except ValueError:
                continue
            if int(process_uid) == want:
                count += 1
        # A caller's own uid must be present. Treat an empty/incomplete ps view
        # as unavailable so plan() disables the fence rather than undercounts.
        return count if count else None
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    n = 0
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != want:
                continue
            # task/ 下每个条目是一个线程。目录读不到（进程刚退出）时
            # 至少记 1，别把一个存在的进程算成 0。
            try:
                n += len(os.listdir(f"/proc/{pid}/task"))
            except OSError:
                n += 1
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
    return n


def plan(reserve_ratio: float = DEFAULT_RESERVE_RATIO,
         *, uid: int | None = None) -> Fence:
    """算这次派发该给 worker 多少进程配额。

    公式：hard 上限 - 已用 = 余量；余量扣掉 reserve_ratio 就是 worker 能用的。
    再加回"已用"，因为 RLIMIT_NPROC 是 uid 级计数，worker 的上限要覆盖
    编排层自己那些进程——只给增量的话 worker 一 fork 就撞墙。
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    except (OSError, ValueError, AttributeError) as exc:
        return Fence(None, f"读不到 RLIMIT_NPROC，不设围栏：{exc}")

    if hard == resource.RLIM_INFINITY:
        return Fence(None, "RLIMIT_NPROC 硬上限是 unlimited，无从计算余量，不设围栏")

    used = current_usage(uid)
    if used is None:
        return Fence(None, "数不出当前 uid 的资源用量，不设围栏")

    headroom = hard - used
    if headroom <= 0:
        return Fence(None, f"已用 {used} 触顶（hard={hard}），不设围栏，机器需要人看")

    grant = int(headroom * (1.0 - reserve_ratio))
    limit = used + grant
    if grant < MIN_VIABLE_LIMIT:
        return Fence(
            None,
            f"余量只剩 {headroom}（hard={hard} used={used}），"
            f"扣掉 {reserve_ratio:.0%} 救护车道后仅 {grant} < {MIN_VIABLE_LIMIT}，"
            f"设了 worker 也起不来，不设围栏",
        )

    # soft 已经比我们算的更严时不要放宽。setrlimit 允许把 soft 升到 hard，
    # 但那是在放松别人定的约束（systemd LimitNPROC、ulimit -u），不该由这里做。
    if soft != resource.RLIM_INFINITY and soft < limit:
        return Fence(
            soft,
            f"沿用已有 soft 上限 {soft}（比算出的 {limit} 更严，不放宽）",
        )

    return Fence(
        limit,
        f"围栏 {limit}（hard={hard} used={used} 留 {reserve_ratio:.0%} 救护车道）",
    )
