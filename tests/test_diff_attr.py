"""`.gitattributes` 关掉 diff 正文 —— 监工审的是一份空白。

洞的形状：两个模型监工（spec / architecture）审的是 `git diff HEAD` 的
**正文**。`.gitattributes` 里一行 `*.py -diff` 让 git 把 py 文件当二进制，
diff 正文退化成 "Binary files a/a.py and b/a.py differ"。而 changed_paths
不受影响 —— 范围监工和 runbook 照常工作。三道闸门里两道正常，所以难发现。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.harness.workspace import capture_diff, diff_suppressed


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "uv.lock").write_text("locked = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_diff_body_degrades_to_binary(tmp_path: Path) -> None:
    """`*.py -diff` 之后，监工在 diff 里看不到那行 pdb。"""
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")

    diff, _ = capture_diff(root)

    assert "pdb.set_trace" not in diff, "洞不存在了？那这个检测可以删"
    assert "Binary files" in diff


def test_the_hole_leaves_changed_paths_intact(tmp_path: Path) -> None:
    """同一次改动里，范围监工和 runbook 用的 changed_paths 完全正常。

    这是这个洞最难被发现的地方：三道闸门里两道行为正常。
    """
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")

    _, paths = capture_diff(root)

    assert "a.py" in paths
    assert ".gitattributes" in paths
    # runbook 是 grep 真实文件，不看 diff，所以照常抓到
    assert "pdb.set_trace" in (root / "a.py").read_text(encoding="utf-8")


def test_the_hole_needs_no_new_file(tmp_path: Path) -> None:
    """`.gitattributes` 已存在时，连新增文件都不需要 —— 追加一行就够。

    所以 runner_hooks 那条「判新增」的检测看不见它。
    """
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.md -diff\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "attrs")

    with (root / ".gitattributes").open("a", encoding="utf-8") as fh:
        fh.write("*.py -diff\n")
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")

    added = _git(root, "diff", "HEAD", "--name-only", "--diff-filter=A")
    assert added.strip() == "", "有新增文件的话就不是这个洞了"
    assert diff_suppressed(root, ("a.py",)) == ("a.py",)


# --- 二、判据：判「这一轮才被关掉」，不判「有没有被关」 --------------------


def test_clean_tree_is_quiet(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert diff_suppressed(root, ("a.py", "uv.lock")) == ()


def test_minus_diff_on_source_is_caught(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    assert diff_suppressed(root, ("a.py",)) == ("a.py",)


def test_custom_diff_driver_is_caught(tmp_path: Path) -> None:
    """`diff=nodiff` 这类自定义 driver 也算 —— 配上 textconv 能让整段消失。"""
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.py diff=nodiff\n", encoding="utf-8")
    assert diff_suppressed(root, ("a.py",)) == ("a.py",)


def test_pre_existing_lockfile_rule_is_not_flagged(tmp_path: Path) -> None:
    """合法用途：仓库本来就写着 `*.lock -diff`，改锁文件不该判红。

    本仓库就有 uv.lock。没有这条基线比对，这道闸门会在每个改锁文件的
    任务上响 —— 一道天天误报的闸门等于一道被关掉的闸门。
    """
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.lock -diff\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "attrs")

    (root / "uv.lock").write_text("locked = 2\n", encoding="utf-8")

    assert diff_suppressed(root, ("uv.lock",)) == ()


def test_lockfile_allowed_and_source_blocked_in_one_round(tmp_path: Path) -> None:
    """同一轮里两者并存：合法那条放过，新加的那条拦下。"""
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.lock -diff\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "attrs")

    with (root / ".gitattributes").open("a", encoding="utf-8") as fh:
        fh.write("*.py -diff\n")
    (root / "uv.lock").write_text("locked = 2\n", encoding="utf-8")
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")

    assert diff_suppressed(root, ("uv.lock", "a.py", ".gitattributes")) == ("a.py",)


def test_re_enabling_diff_is_not_flagged(tmp_path: Path) -> None:
    """反方向：worker 把 `-diff` 改回正常，不该判红。"""
    root = _repo(tmp_path)
    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "attrs")

    (root / ".gitattributes").write_text("", encoding="utf-8")

    assert diff_suppressed(root, ("a.py",)) == ()


def test_empty_paths_spawns_no_git(tmp_path: Path, monkeypatch) -> None:
    """空 paths 时一个 git 都不 fork。

    刻意断言「没 fork」而不只断言「返回空」：后者在早返回被删掉之后**照样
    通过** —— `git check-attr diff --` 不读 stdin，立刻 129 退出，函数里
    `returncode != 0` 那条路把它变成同一个空结果（一次变异实测存活）。
    一条删掉代码也不会红的测试，等于没写。
    """
    import factory.harness.workspace as mod

    root = _repo(tmp_path)
    calls: list = []
    real = mod._git
    monkeypatch.setattr(
        mod, "_git", lambda r, *a: (calls.append(a), real(r, *a))[1]
    )

    assert diff_suppressed(root, ()) == ()
    assert calls == []


def test_path_containing_the_delimiter(tmp_path: Path) -> None:
    """文件名里带 `: diff: ` 时不能把路径切断。rpartition 的方向就是为这个。"""
    root = _repo(tmp_path)
    weird = "weird: diff: name.py"
    (root / weird).write_text("y = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "weird")

    (root / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")

    assert diff_suppressed(root, (weird,)) == (weird,)


def test_non_repo_returns_empty(tmp_path: Path) -> None:
    """不是仓库时闭嘴，不抛。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert diff_suppressed(plain, ("a.py",)) == ()


def test_this_repo_is_quiet() -> None:
    """本仓库自己（有 uv.lock）在真实路径上不该响。"""
    here = Path(__file__).resolve().parent.parent
    tracked = _git(here, "ls-files").splitlines()[:60]
    assert diff_suppressed(here, tuple(tracked)) == ()


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, attrs: str | None = None):
    """跑一轮真 dispatcher。`attrs` 模拟 worker 在派发**期间**写 .gitattributes。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class AttrWriting(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if attrs is not None:
                (Path(workspace) / ".gitattributes").write_text(attrs, encoding="utf-8")
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-da",
        prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=AttrWriting([_result(paths=("a.py", ".gitattributes"))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")
    return ws


def test_suppressing_diff_blocks_the_merge(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    assert rep.outcome.value != "merged", f"关掉 diff 却合并了：{rep}"


def test_the_claim_names_the_path(tmp_path: Path) -> None:
    """打回的话得让 worker 知道动哪个文件，光说「diff 被关了」他不知道改哪。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    assert "a.py" in rep.escalation_reason
    assert ".gitattributes" in rep.escalation_reason


def test_nothing_is_committed_when_diff_was_suppressed(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    head_before = _git(ws, "rev-parse", "HEAD").strip()
    _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    assert _git(ws, "rev-parse", "HEAD").strip() == head_before


def test_checks_do_not_run_when_diff_was_suppressed(tmp_path: Path) -> None:
    """这道闸门在 check 之前 —— 已知监工看不见的树上没必要再花一次 check 的钱。"""
    ran: list = []
    ws = _worktree(tmp_path)
    _dispatch(tmp_path, ws, ran, attrs="*.py -diff\n")
    assert ran == [], f"拦下之后还跑了 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """没动 .gitattributes 的正常一轮照常合并 —— 否则这就是个全局阻塞。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs=None)
    assert rep.outcome.value == "merged", rep.escalation_reason


def test_the_claim_is_distinct_from_the_other_gates(tmp_path: Path) -> None:
    """四道闸门四个 claim：worker 的补救动作各不相同，糊成一条会白烧一轮。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    reasons = rep.escalation_reason
    assert "diff-suppressed" in reasons
    assert "shadow-code" not in reasons
    assert "runner-hook-added" not in reasons
    assert "git-hook-touched" not in reasons
