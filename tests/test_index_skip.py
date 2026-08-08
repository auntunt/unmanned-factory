"""索引跳过标记 —— 让改动从 git 眼里彻底消失。

`git update-index --assume-unchanged` / `--skip-worktree` 之后，git 对这个
文件的工作区改动一概不看：`git diff HEAD` 里没有它，`--name-only` 里也没有。

比第七个洞（`.gitattributes -diff`）更彻底：那个只让 diff **正文**退化成
"Binary files … differ"，路径还在 changed_paths 里，范围监工和 runbook 照常
工作。这个把路径本身抹掉。而 check 跑在真实文件树上 —— 藏起来的那行会执行。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import (
    capture_diff,
    diff_suppressed,
    index_skipped,
    newly_skipped,
    runner_hooks,
    shadow_code,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_hides_the_change_from_every_gate(tmp_path: Path) -> None:
    """完整攻击：可见地改坏源码，隐蔽地把测试改成永绿。"""
    root = _repo(tmp_path)
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a - b   # 需求是和，这里是差\n", encoding="utf-8"
    )
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert True\n", encoding="utf-8"
    )
    _git(root, "update-index", "--assume-unchanged", "tests/test_calc.py")

    diff, paths = capture_diff(root)

    assert paths == ("calc.py",), f"测试文件还是被看见了：{paths}"
    assert "test_calc" not in diff
    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert diff_suppressed(root, paths) == ()

    proc = subprocess.run(
        ("python", "-m", "pytest", "-q", "--no-header"),
        cwd=root, capture_output=True, text=True,
    )
    assert proc.returncode == 0, "check 没绿？那这个洞的危害要重新写"


def test_the_hole_cannot_ship_unreviewed_code(tmp_path: Path) -> None:
    """危害的边界：land 提交不了这类文件，所以不是「未审代码出货」。

    钉这条是因为它决定了闸门的位置和 claim 的措辞 —— 保的是那份绿的可信度。
    """
    root = _repo(tmp_path)
    (root / "calc.py").write_text("x = 1\nimport pdb\n", encoding="utf-8")
    _git(root, "update-index", "--assume-unchanged", "calc.py")

    _git(root, "add", "-A")
    proc = subprocess.run(
        ("git", "commit", "-qm", "landed"), cwd=root, capture_output=True, text=True
    )
    assert proc.returncode != 0, "居然提交进去了 —— 危害比记录的更大，得重写"
    assert "pdb" in (root / "calc.py").read_text(encoding="utf-8")


# --- 二、判据 -------------------------------------------------------------


def test_clean_repo_has_no_flags(tmp_path: Path) -> None:
    assert index_skipped(_repo(tmp_path)) == frozenset()


def test_assume_unchanged_is_caught(tmp_path: Path) -> None:
    """小写首字母。"""
    root = _repo(tmp_path)
    _git(root, "update-index", "--assume-unchanged", "calc.py")
    assert index_skipped(root) == frozenset({"calc.py"})


def test_skip_worktree_is_caught(tmp_path: Path) -> None:
    """`S` 首字母。两个标记效果一样致命，只查一个等于漏一半。"""
    root = _repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "calc.py")
    assert index_skipped(root) == frozenset({"calc.py"})


def test_both_flags_on_one_path_counted_once(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "update-index", "--assume-unchanged", "--skip-worktree", "calc.py")
    assert index_skipped(root) == frozenset({"calc.py"})


def test_a_path_with_spaces_survives_parsing(tmp_path: Path) -> None:
    """`ls-files -v` 的格式是 `<tag><空格><path>`，路径里可以有空格。"""
    root = _repo(tmp_path)
    d = root / "dir with space"
    d.mkdir()
    (d / "c.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add")
    _git(root, "update-index", "--skip-worktree", "dir with space/c.py")
    assert index_skipped(root) == frozenset({"dir with space/c.py"})


def test_pre_existing_flags_are_not_flagged(tmp_path: Path) -> None:
    """派发前就挂着的标记不报。有人拿它忽略本地配置改动，那是合法用法。"""
    root = _repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "calc.py")
    before = index_skipped(root)
    assert newly_skipped(before, index_skipped(root)) == ()


def test_removing_a_flag_is_not_flagged(tmp_path: Path) -> None:
    """取消标记不报 —— 那是往闸门视野里**送**东西，不是藏东西。

    刻意不像 changed_hooks 那样取对称差：hook 被删掉等于关掉一道闸门，
    而标记被取消等于打开一道视野。方向相反，判据不该照抄。
    """
    root = _repo(tmp_path)
    _git(root, "update-index", "--assume-unchanged", "calc.py")
    before = index_skipped(root)
    _git(root, "update-index", "--no-assume-unchanged", "calc.py")
    assert newly_skipped(before, index_skipped(root)) == ()


def test_a_new_flag_on_top_of_an_old_one_is_flagged(tmp_path: Path) -> None:
    """已经有一个合法标记的仓库里，新挂的那个还是要报。"""
    root = _repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "calc.py")
    before = index_skipped(root)
    _git(root, "update-index", "--assume-unchanged", "tests/test_calc.py")
    assert newly_skipped(before, index_skipped(root)) == ("tests/test_calc.py",)


def test_non_repo_returns_empty(tmp_path: Path) -> None:
    """不是仓库时静默返回空，不抛 —— 和别的检测同一个约定。"""
    assert index_skipped(tmp_path) == frozenset()


def test_this_repo_is_clean(tmp_path: Path) -> None:
    """本仓库 106 个文件首字母全是 H。零误报面，这道闸门才装得上。"""
    assert index_skipped(Path(__file__).resolve().parents[1]) == frozenset()


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, flag: str | None = None):
    """跑一轮真 dispatcher。`flag` 模拟 worker 在派发**期间**挂标记。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class Flagging(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if flag is not None:
                subprocess.run(
                    ("git", "update-index", "--assume-unchanged", flag),
                    cwd=workspace, capture_output=True, check=True,
                )
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-isk", prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=Flagging([_result(paths=("calc.py",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return ws


def test_flagging_a_path_blocks_the_merge(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], flag="tests/test_calc.py")
    assert rep.outcome.value != "merged", f"挂了标记却合并了：{rep}"
    assert "index-skip-flag" in rep.escalation_reason


def test_the_claim_names_the_flagged_path(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], flag="tests/test_calc.py")
    assert "tests/test_calc.py" in rep.escalation_reason
    for other in ("shadow-code", "runner-hook-added", "diff-suppressed",
                  "spec-criteria-mutated"):
        assert other not in rep.escalation_reason


def test_checks_do_not_run_when_a_path_is_flagged(tmp_path: Path) -> None:
    """闸门在跑 check 之前 —— 它伤的正是那份绿。"""
    ran: list = []
    ws = _worktree(tmp_path)
    _dispatch(tmp_path, ws, ran, flag="tests/test_calc.py")
    assert ran == [], f"路径被藏起来了还去跑 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    ran: list = []
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, ran, flag=None)
    assert rep.outcome.value == "merged", f"干净的一轮被拦了：{rep}"
    assert ran and ran[0][0] == "ok"


def test_review_refuses_to_run_without_the_baseline(tmp_path: Path) -> None:
    """`skipped_before` 没有默认值。三个基线是同一个教训的三份。"""
    import inspect

    from factory.dispatcher import Dispatcher

    p = inspect.signature(Dispatcher._review).parameters["skipped_before"]
    assert p.default is inspect.Parameter.empty


def test_a_pre_existing_flag_does_not_block_the_merge(tmp_path: Path) -> None:
    """派发**前**就挂着标记的仓库，干净的一轮照常合并。

    这条是 M83 补出来的：把闸门改成「不看基线，有标记就报」，前面 17 条
    全过 —— 因为 `test_pre_existing_flags_are_not_flagged` 测的是
    `newly_skipped` 这个函数，没测闸门在这种仓库上的行为。
    测函数不等于测接线，本会话第三回。

    合法用法真实存在：有人拿 --skip-worktree 忽略本地 config 的改动。在那种
    仓库上每个任务都判红 = 这道闸门等于被关掉。
    """
    ran: list = []
    root = _repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "calc.py")
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    # worktree 有自己的索引，标记得在这边挂 —— 挂在父仓库上传不过来。
    subprocess.run(
        ("git", "update-index", "--skip-worktree", "calc.py"),
        cwd=ws, capture_output=True, check=True,
    )
    (ws / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    assert index_skipped(ws) == frozenset({"calc.py"}), "标记没挂上，这条测不到东西"
    d = Dispatcher(
        adapter=FakeAdapter([_result(paths=("tests/test_calc.py",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    rep = d.run(
        Task(task_id="T-pre", prompt="p", checks=(CheckSpec(name="ok", command="true"),)),
        ws,
    )
    assert rep.outcome.value == "merged", f"本来就挂着标记的仓库被拦了：{rep}"
    assert ran and ran[0][0] == "ok"
