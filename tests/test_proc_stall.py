"""停滞检测：worker 活着但不吐字节要被提前杀掉。

为什么用真进程而不是 mock：这个功能的全部价值在于「区分挂死和慢」，
而这个区分只存在于真实的时间维度上。mock 掉 Popen 就等于把被测的东西
换成了测试自己的假设。判据必须是行为的 —— 之前用「搜源码关键字」写过
一版判据，被 changed_paths 里的 `c-hang-ed` 命中而恒真。
"""

from __future__ import annotations

import sys
import time

import pytest

from factory.harness.proc import Timeout, run_bounded

# 假 worker 的三种形态。写成 -c 串而不是临时文件：少一层 tmpdir 清理，
# 而且失败时 pytest 的报错里直接能看到进程干了什么。
_HANG = "import time; time.sleep(600)"
_CHATTY = ("import time\n"
           "for i in range(20):\n"
           "    print(i, flush=True)\n"
           "    time.sleep(0.2)\n")


def _run(code: str, **kw):
    return run_bounded([sys.executable, "-c", code], **kw)


def test_stalled_worker_killed_before_timeout():
    """不吐字节的进程在 stall 阈值处被杀，远早于 timeout_s。"""
    t0 = time.monotonic()
    with pytest.raises(Timeout) as ei:
        _run(_HANG, timeout_s=600, stall_timeout_s=2)
    elapsed = time.monotonic() - t0

    assert ei.value.stalled is True
    # 上限给到 15s：CI 上进程启动和 GRACE_S 的两阶段 kill 都要时间。
    # 关键是它远小于 600s —— 那才是这个功能要消掉的等待。
    assert elapsed < 15, f"{elapsed:.1f}s 太久，没有提前止损"
    assert "stalled" in str(ei.value)


def test_streaming_worker_not_killed():
    """一直有输出的慢任务不能被误杀，且 stdout 要完整。

    这是这个功能最容易写错的一半：按「总时长」杀会让所有慢任务陪葬，
    按「输出增量」杀才只打中挂死的。
    """
    r = _run(_CHATTY, timeout_s=600, stall_timeout_s=2)

    assert r.returncode == 0
    # 20 行全在，一行不少 —— 双线程抽管道不能丢数据。
    assert [int(x) for x in r.stdout.split()] == list(range(20))


def test_real_timeout_not_marked_stalled():
    """跑满 timeout_s 的进程标 stalled=False。

    承重：熔断器靠这个字段区分「环境故障」和「任务太大」。混成一种，
    就会拿模型阶梯去重试一个环境问题，或者反过来把慢任务判成故障。
    """
    with pytest.raises(Timeout) as ei:
        _run(_CHATTY, timeout_s=2, stall_timeout_s=600)

    assert ei.value.stalled is False
    assert "timeout after" in str(ei.value)


def test_exit_code_and_streams_preserved():
    """开了停滞检测之后，正常退出路径的返回值不能变。"""
    r = _run("import sys; print('o'); sys.stderr.write('e'); sys.exit(7)",
             timeout_s=60, stall_timeout_s=30)

    assert (r.returncode, r.stdout.strip(), r.stderr.strip()) == (7, "o", "e")


def test_disabled_by_default_path_unchanged():
    """stall_timeout_s=None 走原来的 communicate 路径，行为不变。

    探针和短命令都不传这个参数，那条路必须一个字节都没动。
    """
    r = _run("print('legacy')", timeout_s=60)

    assert (r.returncode, r.stdout.strip()) == (0, "legacy")
