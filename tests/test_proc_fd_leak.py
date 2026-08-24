"""_reap() 在「后代逃出进程组」时的行为（原 C-2 条目）。

审查报告说这里泄漏 fd，实测不成立：Popen 的管道由 Popen 对象持有，
run_bounded 返回后引用归零就关掉了，连跑 10 次 delta=0。

真问题是**异常类型**：被杀的进程若派生过逃出进程组的后代（os.setsid() 之后
还活着），那个后代继承的管道写端一直开着，_reap() 里的 communicate 永远读不到
EOF 而超时。这条路径实测 10/10 触发，不是罕见分支。此时若让
subprocess.TimeoutExpired 穿出 run_bounded，上层会把「任务超时」误判成
「harness 崩了」—— 契约是超时抛 factory 自己的 Timeout。

所以下面的核心断言是异常类型，fd 计数留作回归护栏。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from factory.harness.proc import Timeout, run_bounded

# 子进程 fork 一个孙子，孙子 setsid() 换进程组逃出 killpg 的打击面，
# 继承着 stdout/stderr 管道写端睡很久；子进程自己立刻退出。
# 这是「communicate 在 SIGKILL 之后仍然超时」的最小复现。
_ESCAPING_CHILD = """
import os, time
pid = os.fork()
if pid == 0:
    os.setsid()          # 逃出进程组 → killpg 打不到
    time.sleep(120)      # 抱着管道写端不放
    os._exit(0)
os._exit(0)
"""


@pytest.fixture
def escaping_script(tmp_path):
    script = tmp_path / "escape.py"
    script.write_text(_ESCAPING_CHILD)
    yield script
    # 清掉逃出去的孙子，否则它们会活到测试会话结束
    subprocess.run(["pkill", "-f", str(script)], capture_output=True)


def _open_fd_count() -> int:
    return len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))


def test_escaped_descendant_still_raises_factory_timeout(escaping_script, tmp_path):
    """后代逃出进程组时，抛的必须是 factory 的 Timeout。

    这是本文件的核心断言。曾经在 _reap() 里补第二次 communicate 而不 catch，
    结果 subprocess.TimeoutExpired 10/10 穿出 run_bounded —— 超时被误判成
    harness 故障。pytest.raises(Timeout) 不会捕获 TimeoutExpired
    （两者无继承关系），所以这条测试对那个回归有区分力。
    """
    with pytest.raises(Timeout):
        run_bounded([sys.executable, str(escaping_script)], cwd=tmp_path, timeout_s=1.0)


def test_escaped_descendant_does_not_leak_fds(escaping_script, tmp_path):
    """走「communicate 也超时」这条路径时不能累积 fd。"""
    if not Path("/proc").is_dir():
        pytest.skip("需要 /proc 来数 fd")

    for _ in range(2):  # 预热：让一次性分配稳定
        with pytest.raises(Timeout):
            run_bounded(
                [sys.executable, str(escaping_script)], cwd=tmp_path, timeout_s=0.5
            )

    before = _open_fd_count()
    for _ in range(8):
        with pytest.raises(Timeout):
            run_bounded(
                [sys.executable, str(escaping_script)], cwd=tmp_path, timeout_s=0.5
            )
    after = _open_fd_count()

    assert after - before <= 2, f"fd 累积：{before} → {after}（8 次调用）"


def test_ordinary_timeout_does_not_leak_fds(tmp_path):
    """普通超时（不派生后代，SIGKILL 就能收干净）也不能泄漏。"""
    if not Path("/proc").is_dir():
        pytest.skip("需要 /proc 来数 fd")

    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"  # 强制走到 SIGKILL
        "time.sleep(300)\n"
    )

    for _ in range(2):
        with pytest.raises(Timeout):
            run_bounded([sys.executable, str(script)], cwd=tmp_path, timeout_s=0.5)

    before = _open_fd_count()
    for _ in range(8):
        with pytest.raises(Timeout):
            run_bounded([sys.executable, str(script)], cwd=tmp_path, timeout_s=0.5)
    after = _open_fd_count()

    assert after - before <= 2, f"fd 累积：{before} → {after}"


def test_normal_exit_does_not_leak_fds(tmp_path):
    """正常退出路径的对照组。"""
    if not Path("/proc").is_dir():
        pytest.skip("需要 /proc 来数 fd")

    before = _open_fd_count()
    for _ in range(12):
        run_bounded([sys.executable, "-c", "print('ok')"], cwd=tmp_path, timeout_s=10.0)
    after = _open_fd_count()
    assert after - before <= 2, f"正常退出也泄漏：{before} → {after}"


def test_killed_process_leaves_no_zombie(tmp_path):
    """被杀的直接子进程必须被回收，不留僵尸。"""
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(300)\n"
    )

    with pytest.raises(Timeout):
        run_bounded([sys.executable, str(script)], cwd=tmp_path, timeout_s=0.5)

    out = subprocess.run(
        ["ps", "-o", "stat=", "--ppid", str(os.getpid())],
        capture_output=True,
        text=True,
    )
    zombies = [s for s in out.stdout.split() if s.startswith("Z")]
    assert not zombies, f"留下僵尸进程：{zombies}"
