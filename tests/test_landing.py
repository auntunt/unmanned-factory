"""落地：判绿的产出提交到任务分支，spec §5 的 commit 字段不再永远是 None。

这层的风险全在「提交到哪」。所以测试的重心不是「能不能提交成功」，
而是**不该提交的时候一定不提交**。
"""

import subprocess
from pathlib import Path

import pytest

from factory.harness.landing import (
    FALLBACK_EMAIL, Landing, is_linked_worktree, land,
)


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True)


@pytest.fixture
def repo(tmp_path):
    """一个有基线 commit 的仓库，身份配在仓库级（不碰全局配置）。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def wt(repo, tmp_path):
    """repo 的一棵 linked worktree，就是 dispatcher 会拿到的那种 workspace。"""
    path = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "factory/t-1", str(path), "HEAD")
    return path


# ---------- 只在 linked worktree 里提交 ----------

def test_a_commit_is_never_made_in_the_main_worktree(repo):
    """主工作树一律拒绝 —— 这是防线不是优化。

    `factory run` 不加 --worktree 时 workspace 就是人的仓库本身，所以这条
    路径是**默认会走到**的，不是边角情况。人的检出目录里冒出一个没人
    要求过的 commit，是这一层能造成的最坏后果。
    """
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "new.py").write_text("x = 1\n")

    got = land(repo, task_id="T-1", attempt_no=1)

    assert got.commit is None
    assert "linked worktree" in got.reason
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    # 改动还在，只是没被提交 —— 拒绝提交不等于丢弃产出。
    assert (repo / "new.py").exists()


def test_the_refusal_does_not_leave_things_staged(repo):
    """拒绝的那条路径必须在 `git add` 之前返回。

    先 add 再检查会把人工作目录的 index 弄脏：人下一次 `git commit` 会
    连带提交 agent 的改动，而他以为自己只提交了手写的那部分。
    """
    (repo / "new.py").write_text("x = 1\n")
    land(repo, task_id="T-1", attempt_no=1)
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
    assert staged == "", f"index 被弄脏了：{staged}"


def test_a_linked_worktree_is_recognised(repo, wt):
    assert is_linked_worktree(wt)
    assert not is_linked_worktree(repo)


def test_a_non_repo_directory_is_not_a_worktree(tmp_path):
    # 判据读的是 git 的返回码，不是目录结构 —— 非仓库要安全地返回 False
    # 而不是抛，因为调用点在 dispatcher 判绿之后，抛会把绿的判决弄没。
    assert not is_linked_worktree(tmp_path)


# ---------- 提交本身 ----------

def test_landing_commits_on_the_task_branch_only(repo, wt):
    """产出提交到 factory/* 分支，主分支和父仓库工作区都不动。

    这是整层能成立的前提。实测过一次（/tmp 探针），这里钉住它 ——
    哪天 git 的 worktree 语义变了，或者有人把 land 改成在 repo 上跑，
    应该是这条挂。
    """
    main_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (wt / "feature.py").write_text("def f(): return 1\n")

    got = land(wt, task_id="T-1", attempt_no=1)

    assert got.commit and len(got.commit) == 40
    assert bool(got) is True
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == got.commit
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() \
        == "factory/t-1"
    # 主分支没动，父仓库工作区干净
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == main_head
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_new_files_in_new_directories_are_committed(wt):
    # `git add -A` 而不是 `git add <paths>`：agent 常常新建整个目录，
    # 而按 changed_paths 逐个 add 会漏掉它没报告的那些文件。
    (wt / "pkg").mkdir()
    (wt / "pkg" / "__init__.py").write_text("")
    (wt / "pkg" / "core.py").write_text("X = 1\n")

    assert land(wt, task_id="T-1", attempt_no=1)
    names = _git(wt, "show", "--name-only", "--format=", "HEAD").stdout
    assert "pkg/core.py" in names and "pkg/__init__.py" in names


def test_the_commit_message_carries_the_task_id_for_lookup(wt):
    """trailer 是回查的近路：git blame 拿到 sha，git show 就能读到 task_id。

    审计库仍是权威，但要求人先查库才能知道一行代码属于哪个任务，
    等于给漏报回查加一道摩擦。
    """
    (wt / "a.py").write_text("A = 1\n")
    land(wt, task_id="T-add-slugify", attempt_no=2)
    body = _git(wt, "show", "-s", "--format=%s%n%b", "HEAD").stdout
    assert "T-add-slugify" in body
    assert "Factory-Task: T-add-slugify" in body
    assert "Factory-Attempt: 2" in body


def test_nothing_to_commit_is_not_an_error(wt):
    # 判绿但零改动是可能的（比如 check 本来就绿）。这不是故障，
    # 所以返回的是「没落地 + 一句说明」，不是异常。
    got = land(wt, task_id="T-1", attempt_no=1)
    assert got.commit is None
    assert "没有改动" in got.reason
    assert bool(got) is False


# ---------- 身份与失败 ----------

def test_an_existing_identity_is_used_as_is(wt):
    """仓库配了身份就用它，不注入兜底的 factory@localhost。

    改人的 git 身份是越权的：共享仓库上会把后续所有手工提交的作者也改掉。
    """
    author = _git(wt, "log", "-1", "--format=%ae")  # 基线是 t@example.com
    (wt / "a.py").write_text("A = 1\n")
    land(wt, task_id="T-1", attempt_no=1)
    assert _git(wt, "log", "-1", "--format=%ae").stdout.strip() \
        == "t@example.com" != FALLBACK_EMAIL
    assert author.returncode == 0


def test_the_fallback_identity_is_never_written_to_config(repo, wt):
    """兜底身份只通过 `-c` 传给单条命令，不落任何 config 文件。

    落 config 的后果是无人循环悄悄改了人的仓库设置 —— 而这个改动
    在 diff 里看不见（.git/config 不被版本控制）。
    """
    (wt / "a.py").write_text("A = 1\n")
    land(wt, task_id="T-1", attempt_no=1)
    text = (repo / ".git" / "config").read_text()
    assert FALLBACK_EMAIL not in text


def test_a_hook_that_rejects_the_commit_is_reported_not_raised(repo, wt):
    """pre-commit hook 挂掉 = 提交失败，不是异常。

    调用点在 dispatcher 判绿之后：抛异常会把一个已经全绿的判决变成崩溃。
    这里的正确后果是 commit 字段仍为 None，也就是退回这个功能存在之前。
    """
    # hook 装在 **common dir** 下，不是 worktrees/<name>/hooks ——
    # linked worktree 的 hooksPath 默认指向共用的那份。装错地方会让这个测试
    # 「通过」得毫无意义（hook 根本没跑，提交当然成功）。
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n")
    hook.chmod(0o755)

    (wt / "a.py").write_text("A = 1\n")
    got = land(wt, task_id="T-1", attempt_no=1)

    assert got.commit is None
    assert "git commit 失败" in got.reason
    # hook 没被 --no-verify 绕过 —— 仓库的规则该跑就跑
    assert "nope" in got.reason


def test_landing_is_falsy_when_it_did_not_commit():
    assert not Landing(None, "任何理由")
    assert Landing("a" * 40)


def test_a_repo_with_no_identity_anywhere_still_lands(tmp_path, monkeypatch):
    """身份完全没配也要能提交 —— 否则 CI 容器里落地永远失败。

    上面那个 fixture 的仓库配了身份，所以走不到兜底。这里把全局配置也
    隔掉（GIT_CONFIG_GLOBAL/SYSTEM 指向空文件），才是无人环境的真实样子。
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "empty"))
    (tmp_path / "empty").write_text("")

    root = tmp_path / "bare-id"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "base.txt").write_text("b\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@x", "commit",
         "-q", "-m", "base")
    path = tmp_path / "wt2"
    _git(root, "worktree", "add", "-q", "-b", "factory/t-2", str(path), "HEAD")

    (path / "a.py").write_text("A = 1\n")
    got = land(path, task_id="T-2", attempt_no=1)

    assert got.commit, got.reason
    assert _git(path, "log", "-1", "--format=%ae").stdout.strip() \
        == FALLBACK_EMAIL
    # 兜底身份没有落进任何 config 文件
    assert FALLBACK_EMAIL not in (root / ".git" / "config").read_text()


# ---------- 提交的必须正好是监工审过的 ----------

def test_only_the_reviewed_paths_are_committed(wt):
    """真跑抓到的缺陷：`add -A` 把 check 自己生成的 .pyc 也提交了。

    后果不是「多了几个垃圾文件」，而是审计的两个字段开始描述不同的东西 ——
    diff_hash 只覆盖 src/text.py，commit 里却多了两个 .pyc。
    监工审的是前者，出货的是后者，中间那段差额没有任何人看过。
    """
    (wt / "src").mkdir()
    (wt / "src" / "text.py").write_text("def f(): return 1\n")
    # check 命令跑 `python3 -c "from src.text import f"` 留下的副产物
    cache = wt / "src" / "__pycache__"
    cache.mkdir()
    (cache / "text.cpython-312.pyc").write_bytes(b"\x00fake bytecode")

    got = land(wt, task_id="T-1", attempt_no=1, paths=("src/text.py",))

    assert got.commit, got.reason
    names = _git(wt, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["src/text.py"], f"提交了监工没审过的东西：{names}"
    # .pyc 还在工作区里（没被删，只是没提交）—— 删它不是这一层的事
    assert (cache / "text.cpython-312.pyc").exists()


def test_a_deleted_file_still_lands(wt):
    # 删除也是改动。`add -- <path>` 对已删除的文件同样有效，
    # 用 --ignore-removal 会让「这个任务删掉了一个文件」永远进不了 commit。
    (wt / "gone.py").write_text("x = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "add gone")
    (wt / "gone.py").unlink()

    got = land(wt, task_id="T-1", attempt_no=1, paths=("gone.py",))

    assert got.commit, got.reason
    assert "gone.py" not in _git(wt, "ls-files").stdout.split()


def test_no_paths_falls_back_to_everything(wt):
    """paths 为空时退化成 add -A —— 不报 changed_paths 的 harness 仍能落地。

    这是刻意的退化：那条路径上「提交多了」好过「什么都没提交」，
    因为 diff_hash 在那里本来也是全量算的。
    """
    (wt / "a.py").write_text("A = 1\n")
    (wt / "b.py").write_text("B = 1\n")
    assert land(wt, task_id="T-1", attempt_no=1)
    names = _git(wt, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(names) == ["a.py", "b.py"]


def test_a_path_the_agent_never_touched_commits_nothing(wt):
    # changed_paths 里报了一个其实没改的文件 → 没有改动可提交，不是崩溃。
    got = land(wt, task_id="T-1", attempt_no=1, paths=("base.txt",))
    assert got.commit is None and "没有改动" in got.reason
