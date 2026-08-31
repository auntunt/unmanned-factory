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
import subprocess
import sys
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


#: 哪些测试模块用 `tests.test_dispatcher._task`（它写死了 spec_ref=AC-1 +
#: spec_doc=prd.md）。这些模块的 workspace 里必须有那条的正文，否则
#: dispatcher 在派发前就把任务拦成 escalated（悬空 spec_ref）。
#: 放在 conftest 而不是各模块里：`_task` 是跨模块共用的，配它的 PRD 也该跟着走，
#: 否则下一个 import `_task` 的模块又会撞同一堵墙，而报错（outcome 变
#: escalated）跟 spec_ref 一个字都不沾。
_NEEDS_PRD = ("test_dispatcher", "test_scope", "test_dispatcher_four")


@pytest.fixture(autouse=True)
def _prd_for_shared_task(request, tmp_path):
    """给用共用 `_task` 的模块在 workspace 根写一份能查到 AC-1 的 PRD。

    PRD 是 **workspace 的状态**，不是 Task 构造的一部分 —— 任务说「我要满足
    AC-1」，文档说「AC-1 是什么」。所以是 autouse 夹具，不是 `_task` 的默认值。
    """
    if request.module.__name__.rpartition(".")[2] in _NEEDS_PRD:
        (tmp_path / "prd.md").write_text(
            "# PRD\n\n## 验收标准\n\n- AC-1: 必须返回 str\n", encoding="utf-8")


#: 起 pytest 子进程时必须用的解释器。
#:
#: 不能写裸 "python"：那会走 PATH，解析到的很可能不是正在跑这套测试的解释器。
#: 实测踩过——用 .venv/bin/python -m pytest 跑全套时，子进程里的 "python"
#: 落到了另一个 venv 上，报 "No module named pytest"，于是一批「先证明洞存在」
#: 的前置断言集体假红（基线红→打补丁→期待变绿，而它是因为 pytest 根本没起来
#: 才一直红）。报错里只有 returncode，看不出是解释器问题，极难查。
#:
#: sys.executable 就是当前这个解释器，子进程于是和父进程同环境。
PY = sys.executable

#: 上面那条的 shell 串版本（有些测试把检查命令当字符串喂给 shell）。
PYTEST_CMD = f"{PY} -m pytest"


def git_setup(root: Path) -> None:
    """在 root 建一个有首次提交的 git 仓库。

    共用它是因为四个「假绿洞」测试模块（test_fake_green / test_index_skip /
    test_runner_hooks / test_staged_gitlinks）各自抄了一遍建仓代码，而且抄的是
    不看返回码的 lambda 版：git init 失败也静默继续，然后在后面某条断言上莫名
    报红。这些模块专门在查「worker 自证的绿不可信」，自己却会因为环境抖动假红，
    讽刺得刚好。

    两条规矩：
      1. 每条 git 都有 timeout —— 机器上并行有活时 git 会卡在 index.lock 上，
         不设上限会挂住整个会话。
      2. 失败就带着 stderr 立刻停 —— 建仓是所有用例的前提，前提塌了要在这里报，
         不能让它伪装成被测逻辑的问题。
    """
    # cwd 不存在时 subprocess 在 fork 前就抛 FileNotFoundError，那个报错只说
    # 「没有这个文件」、不说是 cwd 还是 git 本身，而「忘了 mkdir」恰恰是这里
    # 最常见的用错方式。先自己检查，给一句能直接照着改的话。
    if not root.is_dir():
        raise AssertionError(
            f"建仓失败：{root} 不是已存在的目录 —— git_setup 不负责创建它，"
            "调用方要先 mkdir(parents=True)"
        )

    steps = (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"),
    )
    for args in steps:
        try:
            p = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:  # pragma: no cover - 只在机器过载时走到
            raise AssertionError(
                f"建仓卡住：git {' '.join(args)} 在 {root} 超过 60s —— 环境问题，非被测逻辑"
            ) from None
        except FileNotFoundError as exc:  # pragma: no cover - 没装 git 才会走到
            raise AssertionError(f"建仓失败：起不了 git 进程（{exc}）") from None
        if p.returncode != 0:
            raise AssertionError(
                f"建仓失败：git {' '.join(args)} 在 {root} (exit {p.returncode})\n"
                f"stderr: {p.stderr.strip()}"
            )


@pytest.fixture
def spec_doc():
    """往 workspace 里写一份 PRD，返回它的相对路径（给 Task.spec_doc 用）。

    `spec_ref` 非空的任务必须配 `spec_doc`，否则 dispatcher 在派发前就拦下了
    —— 编号不是标准（见 factory/spec_doc.py）。凡是要「带规格引用的真实任务」
    的测试都该用这个，而不是各自手搓一份 Markdown：格式一旦改，只改一处。
    """

    def write(workspace: Path, refs: dict[str, str] | None = None,
              *, name: str = "prd.md") -> str:
        body = refs or {"AC-1": "必须返回 str"}
        lines = ["# PRD", "", "## 验收标准", ""]
        lines += [f"- {ref}: {text}" for ref, text in body.items()]
        (workspace / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return name

    return write
