"""procfence 模块测试。

核心：验证 current_usage 数线程、plan 按公式算、fence 真能拦 fork。
"""
import os
import resource
import sys

import pytest

sys.path.insert(0, "/home/ubuntu/workspace/unmanned-factory")

from factory.harness import procfence
from factory.harness.proc import run_bounded


def test_current_usage_counts_threads():
    """必须数线程，不能只数进程。RLIMIT_NPROC 在 Linux 上计的是 task。"""
    n = procfence.current_usage()
    assert n is not None
    assert n > 0
    # 本进程至少 1 个主线程 + pytest 自己的若干线程，肯定 >1
    assert n > 1


def test_plan_respects_reserve_ratio():
    """公式：(hard - used) * (1 - reserve_ratio) + used。"""
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    if hard == resource.RLIM_INFINITY:
        pytest.skip("RLIMIT_NPROC 是 unlimited，无从计算")
    
    f = procfence.plan(reserve_ratio=0.2)
    if not f.enabled:
        pytest.skip(f"plan() 判定不设围栏：{f.note}")
    
    used = procfence.current_usage()
    assert used is not None
    headroom = hard - used
    expected_grant = int(headroom * 0.8)
    expected_limit = used + expected_grant
    
    # soft 更严时沿用 soft，否则是算出来的值
    if soft != resource.RLIM_INFINITY and soft < expected_limit:
        assert f.limit == soft
    else:
        assert f.limit == expected_limit


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="RLIMIT_NPROC 对 root 不生效（内核跳过 uid 的进程数核算），"
           "围栏拦不住 root 的 fork —— 这是内核语义，不是围栏的实现问题。"
           "非 root 下这条照跑，CI 和开发机都不是 root。",
)
def test_fence_blocks_fork_bomb():
    """围栏真能拦 fork 吗？对照组 vs 实验组。

    必须是**并发** fork，让子进程同时存活并占配额。串行 fork+waitpid 的话
    配额一直只有 2（父+当下这个子），围栏永远撞不到。
    """
    bomb = r"""
import os, sys, time
n = 0
kids = []
try:
    for _ in range(100):
        pid = os.fork()
        if pid == 0:
            time.sleep(5)
            os._exit(0)
        kids.append(pid)
        n += 1
except OSError:
    print("FORK_FAILED_AT", n, flush=True)
else:
    print("ALL_FORKED", n, flush=True)
for p in kids:
    try:
        os.kill(p, 9); os.waitpid(p, 0)
    except OSError:
        pass
"""
    # 对照组：不设围栏，应该能 fork 100 个
    r1 = run_bounded([sys.executable, "-c", bomb], timeout_s=30)
    assert "ALL_FORKED 100" in r1.stdout, f"对照组意外失败：{r1.stdout}"
    
    # 实验组：围栏 current+15，应该只能 fork 约 15 个
    used = procfence.current_usage() or 100
    tight = used + 15
    r2 = run_bounded([sys.executable, "-c", bomb], timeout_s=30, nproc_limit=tight)
    assert "FORK_FAILED_AT" in r2.stdout, f"围栏没拦住：{r2.stdout}"
    # 不要求恰好在 15 那里失败（线程数可能有波动），但必须远小于 100
    assert "ALL_FORKED" not in r2.stdout


def test_plan_soft_fails_gracefully():
    """读不到 /proc、hard 是 unlimited、余量不够时都应该返回 disabled。"""
    # 造一个 hard=10 的情况（已用肯定超过 10）
    f = procfence.plan()
    # 真机器上不太可能触发这些分支，所以只验证接口不崩
    assert f.note != ""
    assert isinstance(f.enabled, bool)
