"""WorktreePool 单元测试。

只测 git 操作语义和 pool 契约，不跑 dispatcher。
所有测试在 /tmp 下建真实 git 仓库。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.harness.worktree import WorktreePool, WorktreeError, branch_name


# ── helpers ──────────────────────────────────────────────────────────────

def _init_repo(base: Path) -> Path:
    """建一个有基线 commit 的最小仓库。"""
    repo = base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=repo, check=True)
    (repo / "base.py").write_text("# base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    return repo


# ── branch_name ──────────────────────────────────────────────────────────

def test_branch_name_simple():
    # 前缀 + 可读部分 + 哈希后缀。后缀是 H-4 修复引入的：纯清洗是多对一的，
    # `feat/login` 和 `feat-login` 会撞同一棵树，后来者删掉前者未合并的产出。
    # 唯一性靠哈希，可读性靠 stem，所以这里断言形状而不是确切字符串。
    b = branch_name("T-1")
    assert b.startswith("factory/T-1-")
    assert len(b.removeprefix("factory/T-1-")) == 6


def test_branch_name_sanitises_slashes():
    # 斜杠在 branch 里是层级分隔符，和前缀结合会破坏 git ref 命名空间
    b = branch_name("T/sub/1")
    assert "/" not in b.removeprefix("factory/")


def test_branch_name_sanitises_spaces():
    b = branch_name("my task 1")
    assert " " not in b


# ── WorktreePool.acquire ──────────────────────────────────────────────────

def test_acquire_creates_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")
    assert wt.path.is_dir()
    assert (wt.path / "base.py").exists()


def test_acquire_branch_in_parent_repo(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")
    branches = subprocess.run(
        ["git", "branch"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert wt.branch in branches


def test_worktree_index_isolated_from_parent(tmp_path):
    """worktree 里 git add -A -N 不能污染父仓库 index（实测 2026-08-07）。"""
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")

    (wt.path / "new_file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A", "-N"], cwd=wt.path, check=True)

    parent_status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert parent_status == "", "父仓库不应看到 worktree 里的改动"


def test_acquire_idempotent_on_stale_dir(tmp_path):
    """上一次运行残留的目录 + 分支不应让 acquire 失败。"""
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt1 = pool.acquire("T-1")
    # 模拟上次 acquire 后进程崩了，没有 release
    wt2 = pool.acquire("T-1")  # 不应 raise
    assert wt2.path.is_dir()


def test_acquire_fails_without_baseline(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    with pytest.raises(WorktreeError, match="没有任何 commit"):
        pool.acquire("T-1")


# ── WorktreePool.release ──────────────────────────────────────────────────

def test_release_removes_path(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")
    ok = pool.release(wt, discard=True)
    assert ok
    assert not wt.path.exists()


def test_release_keeps_dirty_tree_by_default(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")

    (wt.path / "agent_out.py").write_text("result = 42\n")
    ok = pool.release(wt, discard=False)   # default
    assert not ok, "有未提交改动时 discard=False 应返回 False"
    assert wt.path.exists(), "路径应保留"


def test_release_discard_true_removes_dirty_tree(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt = pool.acquire("T-1")
    (wt.path / "tmp.py").write_text("pass\n")
    ok = pool.release(wt, discard=True)
    assert ok
    assert not wt.path.exists()


# ── list_paths ────────────────────────────────────────────────────────────

def test_list_paths_empty_before_any_acquire(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    assert pool.list_paths() == ()


def test_list_paths_shows_active_worktrees(tmp_path):
    repo = _init_repo(tmp_path)
    pool = WorktreePool(repo, root=tmp_path / "wt")
    wt1 = pool.acquire("T-1")
    wt2 = pool.acquire("T-2")
    paths = pool.list_paths()
    assert wt1.path in paths
    assert wt2.path in paths
