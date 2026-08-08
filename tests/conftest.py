"""所有测试共用的夹具。

现在只有一个：`leak_probe`。它存在的理由是工厂里有六条会超时的
subprocess 路（两个 harness、回归监工、模型监工、check 探针、入口提取），
每条都需要问同一个问题 —— **超时之后那棵进程树死了没有**。

只断言 error_text / claim 里有「超时」是不够的：那行字是我们自己写的账。
把 run_bounded 换回 subprocess.run，所有只看文本的断言照样全绿，而机器上
留下一地还在花钱的 node 进程。所以这个夹具只看一件事：孙子进程的生死。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LeakProbe:
    """一个「会派生后台孙子进程」的命令，外加事后验尸的办法。"""

    #: 直接喂给 shell 的命令串（回归监工、check 探针走这条）
    command: str
    #: 一个可执行脚本，行为等价（模型那几条路走 argv，需要 binary）
    script: Path
    _marker: Path

    def grandchild_pid(self, timeout_s: float = 2.0) -> int:
        """等孙子把自己的 pid 写进 marker，然后读出来。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._marker.exists():
                text = self._marker.read_text().strip()
                if text:
                    return int(text)
            time.sleep(0.05)
        raise AssertionError(f"孙子进程没写下 pid：{self._marker}")

    def assert_reaped(self, timeout_s: float = 2.0) -> None:
        """孙子必须死。活着就是漏了一棵树。"""
        pid = self.grandchild_pid()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.05)
        # 别把它留给下一条测试
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        raise AssertionError(f"超时之后 {pid} 还活着 —— 整棵树漏了")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _make(tmp_path: Path, name: str) -> LeakProbe:
    marker = tmp_path / f"{name}.pid"
    # `exec sleep` 很关键：不 exec 的话 sh 会自己等着，marker 里记的是
    # 那个 sh 的 pid，而它可能先死，看起来就像树被收干净了。
    inner = f"sh -c 'echo $$ > {marker}; exec sleep 60' &\nsleep 60\n"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\n{inner}", encoding="utf-8")
    script.chmod(0o755)
    return LeakProbe(command=inner.replace("\n", " "),
                     script=script, _marker=marker)


def _cleanup(tmp_path: Path) -> None:
    """测试自己失败时别把孙子留给后面的测试。"""
    for marker in tmp_path.glob("*.pid"):
        try:
            pid = int(marker.read_text().strip())
        except (ValueError, OSError):
            continue
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


import pytest  # noqa: E402  （放在这里是为了让上面那段说明先被读到）


@pytest.fixture
def leak_probe(tmp_path):
    """产出会漏进程的命令 / 脚本，收场时保证不留残留。"""
    made: list[Path] = []

    def make(name: str = "leaky") -> LeakProbe:
        probe = _make(tmp_path, name)
        made.append(tmp_path)
        return probe

    yield make
    for path in made:
        _cleanup(path)
