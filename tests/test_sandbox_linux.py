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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from factory.harness.transcript import find_transcript, projects_root

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


def run_in_sandbox(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """在沙箱里跑一条命令，返回 CompletedProcess。

    env 给的是**覆盖**而不是整份环境：bwrap 自己要 PATH 才找得到 sh。
    transcript 那组测试用它传 HOME —— prepare() 在真实路径上就是这么把
    出口目录交给 claude 的（CLI 只认 $HOME，没有单独的 flag）。
    """
    merged = {**os.environ, **env} if env else None
    proc = subprocess.run(
        argv, cwd=cwd, env=merged, capture_output=True, text=True, timeout=10
    )
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


# --- transcript 出口目录 ---
#
# 沙箱丢 transcript 的机制：claude 把对话写 $HOME/.claude/projects/<slug>/
# <session_id>.jsonl，而第 2 段的 `--tmpfs /home` 把整个 /home 盖掉了 ——
# 沙箱里的 claude 于是写进 tmpfs，沙箱一销毁记录就没了，审计里
# transcript_path 永远是 None，事后无法复盘 worker 跟模型说了什么。
#
# 修法是开一个**专用**出口目录并把它当 $HOME，而不是把 ~/.claude 整个
# bind 进去。下面这组测试盯三件事：出口通（写得进、活得下来）、
# 隔离没被这个洞破坏（宿主 ~/.claude 依然不可见）、每次执行互不可见。


def _transcript_dir() -> Path:
    """造一个出口目录。测试自己负责删 —— prepare() 刻意不删（它是审计现场）。"""
    return Path(tempfile.mkdtemp(prefix="factory-transcript-"))


def test_transcript_dir_is_writable_in_sandbox(tmp_path):
    """出口通了：沙箱内写得进去，且**沙箱退出后宿主还读得到**。

    这是本次修复的核心判据。断言分两半是刻意的：只断言沙箱内 returncode==0
    的话，写进 tmpfs 也会是 0（那正是坏掉时的表现 —— 包装器不报错，
    隔离却是错的），要到沙箱外去 stat 才能分清写的是 bind 还是 tmpfs。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    home = _transcript_dir()
    try:
        # 复刻 claude 的真实布局，连 $HOME 都走 ~ 展开：验的是"把出口目录
        # 当 HOME 交给子进程"这条路，不是"往一个绝对路径里写文件"。
        argv = build_argv(
            ["sh", "-c", 'mkdir -p ~/.claude/projects/slug && '
                         'echo \'{"type":"assistant"}\' > '
                         '~/.claude/projects/slug/sess-1.jsonl && echo written'],
            workspace=ws,
            tmp_dir=tmp_dir,
            transcript_dir=home,
        )
        proc = run_in_sandbox(argv, cwd=ws, env={"HOME": str(home)})
        assert proc.returncode == 0, f"沙箱内写 transcript 失败：{proc.stderr}"
        assert "written" in proc.stdout
        landed = home / ".claude" / "projects" / "slug" / "sess-1.jsonl"
        assert landed.exists(), "写进了 tmpfs：沙箱一销毁 transcript 就没了"
        # find_transcript 是审计真正走的那条路（按 session-id 全局 glob），
        # 所以判据下到它身上，而不是停在"文件在磁盘上"。
        found = find_transcript("sess-1", root=projects_root(home))
        assert found == landed, f"find_transcript 找不到出口里的记录：{found}"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_real_claude_dir_stays_invisible(tmp_path):
    """硬约束：出口目录不许把宿主 ~/.claude 带进沙箱。

    worker 有权改 workspace 里的代码，但不该能读改自己的配置和凭据 ——
    那等于给下次派发留后门。这条测的是"开了出口之后隔离还在"，
    是上面那条的对偶：少了它，把 ~/.claude 整个 bind 进去也能让上面全绿。
    """
    real = Path.home() / ".claude"
    if not real.exists():
        pytest.skip("宿主没有 ~/.claude，这条无从验证")

    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    home = _transcript_dir()
    try:
        argv = build_argv(
            ["sh", "-c", f"test -e {real} && echo VISIBLE || echo hidden"],
            workspace=ws,
            tmp_dir=tmp_dir,
            transcript_dir=home,
        )
        proc = run_in_sandbox(argv, cwd=ws)
        assert "hidden" in proc.stdout, (
            f"宿主 {real} 在沙箱里可见：worker 能读到自己的凭据")
        assert "VISIBLE" not in proc.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_build_argv_transcript_dir_bind_after_tmpfs(tmp_path):
    """transcript_dir 的 --bind 必须出现在 --tmpfs /home 之后（mount 顺序保证）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    transcript_home = Path(tempfile.mkdtemp(prefix="factory-transcript-"))
    try:
        argv = build_argv(
            ["true"],
            workspace=ws,
            tmp_dir=tmp_dir,
            transcript_dir=transcript_home,
        )
        # 找 --tmpfs /home 和 --bind transcript 的位置
        home_tmpfs_idx = next(
            i for i, a in enumerate(argv) if a == "--tmpfs" and argv[i + 1] == "/home"
        )
        bind_idx = next(
            i for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] == str(transcript_home)
        )
        assert bind_idx > home_tmpfs_idx, (
            f"transcript --bind (idx {bind_idx}) 必须在 --tmpfs /home (idx {home_tmpfs_idx}) 之后"
        )
    finally:
        shutil.rmtree(transcript_home, ignore_errors=True)


def test_home_isolation_not_broken_by_transcript_dir(tmp_path):
    """给定 transcript_dir 时，宿主真实 HOME 仍然隔离——沙箱内写 $HOME 不穿透。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    transcript_home = Path(tempfile.mkdtemp(prefix="factory-transcript-"))
    real_home_marker = Path.home() / f".bwtest-isolation-{tmp_path.name}"
    assert not real_home_marker.exists()

    try:
        argv = build_argv(
            ["sh", "-c", f"echo x > {real_home_marker}; echo done"],
            workspace=ws,
            tmp_dir=tmp_dir,
            transcript_dir=transcript_home,
        )
        run_in_sandbox(argv, cwd=ws)
        assert not real_home_marker.exists(), "宿主 HOME 隔离被 transcript_dir 破坏了"
    finally:
        real_home_marker.unlink(missing_ok=True)
        shutil.rmtree(transcript_home, ignore_errors=True)
