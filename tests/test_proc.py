"""超时要杀掉整棵进程树。

这些用例全部检查**孙子进程**，不检查直接子进程 —— 直接子进程连
`subprocess.run(timeout=)` 都会杀，那个行为从来没坏过。坏的是它的后代，
而后代才是花钱的那一半（claude 是个 node 进程，MCP server 和 Bash 工具
跑的每条命令都是它的后代）。

实测记录（2026-08-06）：改之前跑一次 3 任务的 drain，`--timeout 5` 配一个
`sleep 300` 的假 worker，循环停机后 `pgrep -f 'sleep 300'` 还剩 12 个孤儿。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory.harness.proc import Timeout, run_bounded


def _spawner(tmp_path: Path, marker: Path, *, trap: bool = False) -> Path:
    """一个派生出「孙子」然后自己挂住的脚本。

    孙子把自己的 pid 写进 marker 再长睡 —— 测试靠这个 pid 判断它死没死，
    而不是靠 pgrep（pgrep 会撞上机器上别人的 sleep）。
    """
    script = tmp_path / ("spawn_trap.sh" if trap else "spawn.sh")
    body = f"""#!/bin/sh
{"trap '' TERM" if trap else ""}
sh -c 'echo $$ > {marker}; exec sleep 300' &
sleep 300
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _grandchild_pid(marker: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return int(marker.read_text().strip())
        time.sleep(0.05)
    pytest.fail("孙子进程没起来，测试本身有问题")


def test_a_timeout_kills_the_grandchild_not_just_the_child(tmp_path):
    """这条是整个模块存在的理由。"""
    marker = tmp_path / "gc.pid"
    script = _spawner(tmp_path, marker)
    with pytest.raises(Timeout):
        run_bounded([str(script)], cwd=tmp_path, timeout_s=1.0)
    pid = _grandchild_pid(marker)
    # 组信号发出到进程真的消失有个很短的窗口。
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid), f"孙子 {pid} 在超时之后还活着 —— 它还在烧钱"


def test_a_child_that_ignores_term_still_dies(tmp_path):
    """CLI 卡在某个不响应 TERM 的状态里（真见过）不该让它活下来。

    这条断言的是「进程真的死了」，不只是「hard_killed 标了 True」。第一版
    只测那个标志，于是把 SIGKILL 整段删掉之后 9 条测试全过 —— 标志是我们
    自己写的账，进程活着才是事实。
    """
    marker = tmp_path / "gc.pid"
    script = _spawner(tmp_path, marker, trap=True)
    with pytest.raises(Timeout) as got:
        run_bounded([str(script)], cwd=tmp_path, timeout_s=1.0)
    assert got.value.hard_killed is True, "TERM 被忽略了，应该记下动用了 KILL"
    assert "KILL" in str(got.value), "审计里要看得出这是硬杀，不是正常退出"
    pid = _grandchild_pid(marker)
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid), f"trap 掉 TERM 的那棵树里 {pid} 活下来了"


def test_the_timeout_message_still_says_how_long_it_waited(tmp_path):
    """审计里的 error_text 会被 regression 监工按文本匹配 'timeout'。"""
    script = _spawner(tmp_path, tmp_path / "gc.pid")
    with pytest.raises(Timeout) as got:
        run_bounded([str(script)], cwd=tmp_path, timeout_s=1.0)
    msg = str(got.value)
    assert msg.startswith("timeout after 1.0s"), msg


def test_the_factory_does_not_kill_itself(tmp_path):
    """start_new_session 漏了的话 killpg 会打到我们自己的进程组。

    这条测的是「测试进程在 run_bounded 超时之后还活着」—— 听起来荒谬，
    但漏掉 start_new_session 的后患正是这个，而且症状是整个 pytest 无声消失，
    没有任何断言会失败（因为没有断言还能跑）。
    """
    script = _spawner(tmp_path, tmp_path / "gc.pid")
    with pytest.raises(Timeout):
        run_bounded([str(script)], cwd=tmp_path, timeout_s=1.0)
    assert os.getpid() > 0 and _alive(os.getpid())


def test_the_child_runs_in_its_own_process_group(tmp_path):
    """直接钉死机制本身：子进程的 pgid 必须不等于我们的。"""
    out = run_bounded([sys.executable, "-c",
                       "import os; print(os.getpgid(0))"],
                      cwd=tmp_path, timeout_s=30.0)
    assert out.stdout.strip() != str(os.getpgid(0))


def test_a_normal_exit_returns_stdout_stderr_and_code(tmp_path):
    out = run_bounded([sys.executable, "-c",
                       "import sys; print('o'); print('e', file=sys.stderr); "
                       "sys.exit(3)"],
                      cwd=tmp_path, timeout_s=30.0)
    assert out.returncode == 3
    assert out.stdout.strip() == "o" and out.stderr.strip() == "e"


def test_no_timeout_means_no_bound(tmp_path):
    out = run_bounded([sys.executable, "-c", "print('ok')"], cwd=tmp_path)
    assert out.stdout.strip() == "ok"


def test_a_missing_binary_raises_oserror_not_timeout(tmp_path):
    """harness 的 except OSError 分支靠这个 —— 别把「装不上」报成「超时」。"""
    with pytest.raises(OSError):
        run_bounded([str(tmp_path / "nope")], cwd=tmp_path, timeout_s=5.0)


def _own_zombies() -> list[str]:
    """本进程名下处于 Z 状态的子进程。"""
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,stat="],
                         capture_output=True, text=True).stdout
    mine = str(os.getpid())
    return [ln for ln in out.splitlines()
            if len(ln.split()) >= 3
            and ln.split()[1] == mine and ln.split()[2].startswith("Z")]


def test_a_hard_killed_child_is_reaped_not_left_a_zombie(tmp_path):
    """KILL 之后还要 communicate 一次，否则留下僵尸和两个开着的管道。

    第一版这条测的是 /dev/fd 的数量，结果把整段 communicate 删掉之后 9 条
    全过 —— Popen 出作用域时 GC 会把 fd 收走，fd 数根本看不出区别。僵尸
    才是真症状：无人循环连着跑几百个任务，攒够了就是进程表满。
    """
    script = _spawner(tmp_path, tmp_path / "gc.pid", trap=True)
    with pytest.raises(Timeout):
        run_bounded([str(script)], cwd=tmp_path, timeout_s=0.5)
    assert _own_zombies() == [], "硬杀之后留下了没回收的僵尸"
