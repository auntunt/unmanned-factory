"""Linux 沙箱测试（bubblewrap）。

对标 test_sandbox.py（macOS Seatbelt 版），验证：
  - workspace 可写
  - tmp 可写
  - HOME 不可写（tmpfs 覆盖）
  - 工厂代码不可写（tmpfs 覆盖）
  - worktree 场景下 git commit 能过（git-common-dir 绑进去了）
  - 挂载顺序正确（workspace 在工厂代码底下时不被 tmpfs 吃掉）
"""

import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# 条件导入：只有 Linux 才能 import sandbox_linux
if sys.platform.startswith("linux"):
    from factory.harness.sandbox_linux import (
        available,
        build_argv,
        git_dir,
        prepare,
    )
else:
    available = lambda: False  # type: ignore

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not available(),
    reason="bubblewrap 只在 Linux 上有",
)


def run_in_sandbox(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """在沙箱里跑一条命令，返回 CompletedProcess。"""
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=10)
    return proc


def test_workspace_is_writable(tmp_path):
    """workspace 可写。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    argv = build_argv(
        ["sh", "-c", "echo ok > out.txt && cat out.txt"],
        workspace=ws,
        tmp_dir=tmp_dir,
    )
    proc = run_in_sandbox(argv, cwd=ws)
    assert proc.returncode == 0
    assert "ok" in proc.stdout
    assert (ws / "out.txt").read_text().strip() == "ok"


def test_tmp_is_writable(tmp_path):
    """tmp 可写。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    argv = build_argv(
        ["sh", "-c", f"echo tmp_ok > {tmp_dir}/t.txt && cat {tmp_dir}/t.txt"],
        workspace=ws,
        tmp_dir=tmp_dir,
    )
    proc = run_in_sandbox(argv, cwd=ws)
    assert proc.returncode == 0
    assert "tmp_ok" in proc.stdout


def test_home_is_not_writable(tmp_path):
    """HOME 被 tmpfs 覆盖，不可写。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # 尝试写 ~/blocked.txt，应该失败（tmpfs 是空的且没给写权限？不，给了，但数据不落盘）
    # 实际上 tmpfs 是可写的，但与宿主的 $HOME 隔离。验证隔离：在沙箱内写 ~/x，
    # 退出后宿主 HOME 里看不到。
    home_marker = Path.home() / f".bwtest-{tmp_path.name}"
    assert not home_marker.exists()

    argv = build_argv(
        ["sh", "-c", f"echo x > ~/{home_marker.name}; ls ~/ | head -5"],
        workspace=ws,
        tmp_dir=tmp_dir,
    )
    proc = run_in_sandbox(argv, cwd=ws)
    # 沙箱内能写（tmpfs），但退出后宿主 HOME 里没有
    assert not home_marker.exists(), "HOME 隔离失败：沙箱内的写穿透到了宿主"


def test_factory_root_is_not_writable(tmp_path):
    """工厂代码被 tmpfs 覆盖，不可写。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # factory_root 默认是 __file__ 的上三层，也就是仓库根
    factory_root = Path(__file__).resolve().parents[2]
    marker = factory_root / f".bwtest-factory-{tmp_path.name}"
    assert not marker.exists()

    argv = build_argv(
        ["sh", "-c", f"echo x > {marker}; echo done"],
        workspace=ws,
        tmp_dir=tmp_dir,
    )
    proc = run_in_sandbox(argv, cwd=ws)
    # 沙箱内 factory_root 被 tmpfs 盖掉，写不进去（或者写了也是 tmpfs，退出后没了）
    assert not marker.exists(), "factory_root 隔离失败：工厂代码可被 worker 改写"


def test_git_commit_in_worktree(tmp_path):
    """worktree 场景下 git commit 能过（git-common-dir 绑进去了）。"""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@l"], cwd=main_repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main_repo, check=True)
    (main_repo / "f.txt").write_text("v1")
    subprocess.run(["git", "add", "-A"], cwd=main_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=main_repo, check=True)

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feat"],
        cwd=main_repo,
        check=True,
    )

    common = git_dir(wt)
    assert common is not None
    assert common.exists()

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    argv = build_argv(
        [
            "sh",
            "-c",
            "git config user.email t@l && git config user.name t && "
            "echo v2 > f.txt && git add -A && git commit -qm 'in sandbox' && "
            "git log --oneline | head -1",
        ],
        workspace=wt,
        tmp_dir=tmp_dir,
        git_common=common,
    )
    proc = run_in_sandbox(argv, cwd=wt)
    assert proc.returncode == 0, f"git commit 失败：{proc.stderr}"
    assert "in sandbox" in proc.stdout


def test_mount_order_when_workspace_under_factory_root(tmp_path):
    """workspace 在工厂代码底下时，挂载顺序保证 workspace 不被 tmpfs 吃掉。

    模拟自我托管场景：worktree 池默认在 <repo>/.factory-worktrees/<slug>，
    如果先 bind workspace 后 tmpfs factory_root，workspace 会消失。
    正确顺序是：先 tmpfs factory_root，再 bind workspace（后盖前）。
    """
    # 造一个假的 factory_root
    fake_factory = tmp_path / "fake-factory"
    fake_factory.mkdir()
    (fake_factory / "README.md").write_text("工厂代码")

    # workspace 在它底下
    ws = fake_factory / ".factory-worktrees" / "task1"
    ws.mkdir(parents=True)
    (ws / "out.txt").write_text("产出")

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    argv = build_argv(
        ["sh", "-c", "cat out.txt && echo new > new.txt && cat new.txt"],
        workspace=ws,
        tmp_dir=tmp_dir,
        factory_root=fake_factory,
    )
    proc = run_in_sandbox(argv, cwd=ws)
    assert proc.returncode == 0, f"挂载顺序错误，workspace 被 tmpfs 吃掉了：{proc.stderr}"
    assert "产出" in proc.stdout
    assert "new" in proc.stdout
    assert (ws / "new.txt").read_text().strip() == "new"


def test_prepare_returns_correct_env(tmp_path):
    """prepare() 返回的 env 包含 TMPDIR。"""
    ws = tmp_path / "ws"
    ws.mkdir()

    with contextlib.ExitStack() as stack:
        argv, env = prepare(["echo", "hi"], ws, stack)
        assert "TMPDIR" in env
        assert Path(env["TMPDIR"]).exists()
        assert env["TMPDIR"].startswith("/tmp/factory-work-")
