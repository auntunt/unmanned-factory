"""worktree slug 的安全性与单射性（C-1 路径穿越、H-4 slug 碰撞的回归测试）。

这两个 bug 同源：slug 既要安全（不能穿出池外）又要单射（不同 task_id 不能
撞同一棵树）。原实现两点都不满足，且在 branch_name 和 acquire 里各算一遍。
"""

import subprocess

import pytest

from factory.harness.worktree import (
    WorktreeError,
    WorktreePool,
    _slug,
    branch_name,
)


@pytest.fixture
def repo(tmp_path):
    """带一个基线 commit 的 git 仓库，外加一个诱饵文件。"""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@l"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "important.txt").write_text("绝对不能丢")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


# ---------- C-1：路径穿越 ----------

@pytest.mark.parametrize("evil", ["..", ".", "../..", "../../etc", "../../../tmp"])
def test_traversal_task_ids_stay_inside_pool(repo, tmp_path, evil):
    """含路径穿越成分的 task_id 必须落在池内，不能碰到池外任何东西。

    修复前：'..' 让 _force_release() 的 rmtree 清空主仓库（repo 是池的父目录），
    '.' 清空整个池。两者都实测复现过。
    """
    pool_root = tmp_path / "pool"
    pool = WorktreePool(repo, root=pool_root)

    wt = pool.acquire(evil)

    assert wt.path.resolve().is_relative_to(pool_root.resolve())
    assert wt.path.resolve() != pool_root.resolve()
    # 主仓库完好
    assert (repo / "important.txt").exists()
    assert (repo / ".git").exists()


def test_traversal_does_not_destroy_sibling_worktrees(repo, tmp_path):
    """穿越型 task_id 不能删掉池里别的任务的产出。

    修复前 task_id='.' 会 rmtree 整个池，一次干掉所有并行任务的未合并输出。
    """
    pool_root = tmp_path / "pool"
    pool_root.mkdir()
    victim = pool_root / "existing-task"
    victim.mkdir()
    (victim / "output.txt").write_text("别的任务的未合并产出")

    pool = WorktreePool(repo, root=pool_root)
    pool.acquire(".")

    assert (victim / "output.txt").read_text() == "别的任务的未合并产出"


def test_assert_inside_pool_rejects_outside_path(repo, tmp_path):
    """_assert_inside_pool 直接喂池外路径应当抛错。

    这是 rmtree 前的最后一道闸。_slug() 已经保证不会走到这里，但如果日后
    有人新增调用路径绕过 _slug，这道检查还在。
    """
    pool_root = tmp_path / "pool"
    pool = WorktreePool(repo, root=pool_root)

    with pytest.raises(WorktreeError, match="池外"):
        pool._assert_inside_pool(tmp_path / "elsewhere")

    with pytest.raises(WorktreeError, match="池外"):
        pool._assert_inside_pool(pool_root)  # 池根本身 = task_id '.'


def test_prefix_match_is_not_enough(repo, tmp_path):
    """`/pool-evil` 不是 `/pool` 的子路径 —— 前缀匹配的经典坑。"""
    pool_root = tmp_path / "pool"
    pool = WorktreePool(repo, root=pool_root)

    with pytest.raises(WorktreeError, match="池外"):
        pool._assert_inside_pool(tmp_path / "pool-evil" / "x")


# ---------- H-4：slug 单射 ----------

def test_slug_is_injective_on_realistic_ids():
    """不同 task_id 必须得到不同 slug。

    修复前的实测塌缩：
      feat/login、feat-login、feat login → 全是 feat-login
      重构/队列、修复/超时              → 全是 task
    """
    ids = [
        "T-1", "T/1", "T 1", "T@1",
        "feat/login", "feat-login", "feat login",
        "fix/bug-42", "fix-bug-42",
        "重构/队列", "重构-队列", "修复/超时",
    ]
    slugs = [_slug(i) for i in ids]
    assert len(set(slugs)) == len(ids), f"slug 碰撞：{slugs}"


def test_branch_names_are_injective():
    """分支名同样必须单射 —— 它和目录名同源。"""
    ids = ["feat/login", "feat-login", "重构/队列", "重构-队列"]
    branches = [branch_name(i) for i in ids]
    assert len(set(branches)) == len(ids)


def test_slug_never_empty_or_dotted():
    """slug 不能为空、不能是 '.' 或 '..'（git 和文件系统都不接受）。"""
    for tid in ["", ".", "..", "...", "///", "重构", "@@@", "-", "--"]:
        s = _slug(tid)
        assert s, f"{tid!r} 得到空 slug"
        assert s not in {".", ".."}, f"{tid!r} 得到 {s!r}"
        assert "/" not in s, f"{tid!r} 得到含斜杠的 slug {s!r}"


def test_slug_is_stable():
    """同一个 task_id 必须始终得到同一个 slug（幂等）。

    不稳定的话，重启后 recover 找不到上次的树。
    """
    for tid in ["feat/login", "重构/队列", "T-1"]:
        assert _slug(tid) == _slug(tid)


def test_slug_keeps_readable_stem():
    """可读部分要保留，slug 不能退化成纯哈希 —— 人要能在 ls 里认出任务。"""
    assert _slug("feat/login").startswith("feat-login-")
    assert _slug("fix/bug-42").startswith("fix-bug-42-")


def test_slug_length_is_bounded():
    """超长 task_id 不能撑爆文件名上限（255）。"""
    long_id = "a" * 500
    s = _slug(long_id)
    assert len(s) < 100


def test_two_similar_ids_get_separate_worktrees(repo, tmp_path):
    """端到端：feat/login 和 feat-login 各拿一棵树，互不删除。

    这是 H-4 真正的危害 —— acquire() 开头无条件 _force_release()，
    碰撞时后来者会删掉前一个未合并的产出。
    """
    pool = WorktreePool(repo, root=tmp_path / "pool")

    wt1 = pool.acquire("feat/login")
    (wt1.path / "work1.txt").write_text("第一个任务的产出")

    wt2 = pool.acquire("feat-login")

    assert wt1.path != wt2.path
    assert wt1.branch != wt2.branch
    assert (wt1.path / "work1.txt").exists(), "第一个任务的产出被删了"
