"""停滞检测的两条回归护栏。

来历：2026-08-19 把 Limits.stall_timeout_s 默认设成 120.0，投一个真任务，
三轮 haiku/sonnet/opus 的 wall_clock_ms 是 120098/120097/120099 —— 全被
自己的停滞检测杀掉，而且报成 `cannot launch claude: [Errno 9] Bad file
descriptor`，把「我们杀了它」伪装成「它没启动」。

两个独立缺陷，各一条测试盯着：
  1. 默认开启停滞检测，与 `--output-format json`（非流式）根本冲突。
  2. _reap 的 communicate 撞上 _drain 已关闭的管道，OSError 穿出函数。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.harness.base import Limits
from factory.harness.proc import Timeout, run_bounded


def test_stall_detection_is_off_by_default() -> None:
    """默认必须不开停滞检测。

    ClaudeCodeHarness 跑的是 `--output-format json`：claude 全程静默，
    跑完才一次性吐 JSON。这种格式下「无输出增量」是正常状态，不是挂死。
    一旦给了默认值，每个真实任务都会在阈值处被杀。

    这条测试不是在测一个数字，是在钉住「非流式命令不能按增量判活」这个
    约束 —— 谁想改默认值，先解释清楚 json 格式怎么办。
    """
    assert Limits().stall_timeout_s is None, (
        "stall_timeout_s 默认值必须是 None。"
        "harness 用 --output-format json（非流式），开了会杀掉每个真任务。"
    )


def test_silent_long_process_survives_when_detection_off() -> None:
    """全程不输出的进程，在默认配置下不该被杀。

    这是上面那条约束的行为面：模拟 `--output-format json` 的形状 ——
    先长时间静默，最后一次性输出。用默认 Limits 的值去跑，必须活下来。
    """
    # 静默 3 秒再一次性输出，模拟非流式 CLI。
    script = "import time; time.sleep(3); print('{\"result\":\"ok\"}')"
    result = run_bounded(
        ["python3", "-c", script],
        cwd=Path.cwd(),
        timeout_s=60,
        stall_timeout_s=Limits().stall_timeout_s,  # 就是默认值，None
    )
    assert result.returncode == 0
    assert "ok" in result.stdout, "静默期被误杀会导致输出为空"


def test_stall_kill_raises_timeout_not_oserror() -> None:
    """停滞被杀时，抛出的必须是 Timeout，且带 stalled=True。

    契约测试，不是 bug 复现。诚实说明：真实派发里出现过
    `cannot launch claude: [Errno 9] Bad file descriptor`，怀疑是
    _drain 关掉管道后 _reap 的 communicate 撞 EBADF，那个 OSError 穿出
    run_bounded 会被上层 `except OSError` 误判成「进程没启动」。

    但这条路径**在裸机上没能复现**（试过静默进程、主动 os.close(1)/(2)、
    setsid 孙子进程三种形状，回滚 catch 后依然是 Timeout）。所以 _reap
    里那个 `except OSError` 是防御性的，具体触发点仍未定位 —— 可能只在
    bwrap 双层沙箱下出现。

    这条测试守住的是**契约**：不论清理路径内部发生什么，run_bounded 对外
    只抛 Timeout，且 stalled 字段必须在（上层靠它区分环境故障和任务太慢，
    字段丢了熔断就失效）。
    """
    # 起来就不输出、也不退出 —— 停滞检测的目标形状。
    script = "import time; time.sleep(300)"
    with pytest.raises(Timeout) as excinfo:
        run_bounded(
            ["python3", "-c", script],
            cwd=Path.cwd(),
            timeout_s=60,
            stall_timeout_s=1.0,  # 显式开启，这条测的就是开启后的清理路径
        )
    assert excinfo.value.stalled is True, "停滞标记丢了，上层无法熔断"
    assert "stalled" in str(excinfo.value)


def test_reap_survives_process_that_outlives_pipes() -> None:
    """派生了逃出进程组的后代时，清理也不能抛。

    这条覆盖 _reap 的另一条分支（communicate 超时而非 EBADF）：孙子进程
    setsid 后继续持有管道写端，killpg 打不到它，communicate 读不到 EOF。
    两条清理分支都不许把异常漏给上层。
    """
    # fork 一个 setsid 的孙子，它抱着继承来的 stdout 不放。
    script = (
        "import os, time, sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    time.sleep(30)\n"
        "    sys.exit(0)\n"
        "time.sleep(300)\n"
    )
    with pytest.raises(Timeout) as excinfo:
        run_bounded(
            ["python3", "-c", script],
            cwd=Path.cwd(),
            timeout_s=60,
            stall_timeout_s=1.0,
        )
    assert excinfo.value.stalled is True
