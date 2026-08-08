"""HEAD 被移动 —— `git diff HEAD` 的**基线端**。

前面几道闸门盯的都是这条命令的输入（hooks、属性、config、索引标记、标准），
这道盯它对比的那个**点**。worker 自己 `git commit` 一次，HEAD 一移，diff 就
退化成「相对 worker 那次提交的增量」，已提交的部分谁都看不见。

和第十二个洞（索引跳过标记）的区别：那条路上 `git add -A` 不 stage 被标记的
文件，land 以「无文件要提交」失败，未审代码进不了历史。这条路上代码**已经在
历史里**，land 只是往上叠一层 —— 真的出货。

判据是 (哈希, ref) 对，不是只有哈希：只切分支那一支哈希不变，diff 照样诚实，
但 land 的提交落到 worker 那条分支上，人审的原分支看不到它。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import (
    capture_diff,
    changed_config,
    diff_suppressed,
    git_config,
    head_commit,
    head_position,
    index_skipped,
    info_attributes,
    newly_skipped,
    runner_hooks,
    shadow_code,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "feat")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


BACKDOOR = "import pdb; pdb.set_trace()\n"


def _worker_commits_backdoor(root: Path) -> None:
    """worker 干的事：把后门提交掉，另留一处干净改动不提交。"""
    (root / "a.py").write_text("x = 1\n" + BACKDOOR, encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-qm", "wip")
    (root / "README.md").write_text("hi\nchanged\n", encoding="utf-8")


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_committing_truncates_the_diff(tmp_path: Path) -> None:
    """worker 提交后门 → 监工的 diff 里没有它。"""
    root = _repo(tmp_path)
    _worker_commits_backdoor(root)

    diff, paths = capture_diff(root)

    assert "pdb.set_trace" not in diff, "洞不存在了？那这道闸门可以删"
    assert paths == ("README.md",), f"监工只看到没提交的那处: {paths}"


def test_the_hole_backdoor_is_in_history(tmp_path: Path) -> None:
    """而后门确实在 HEAD 里 —— 不是没写成，是被藏进历史了。"""
    root = _repo(tmp_path)
    _worker_commits_backdoor(root)

    assert "pdb.set_trace" in _git(root, "show", "HEAD:a.py")


def test_the_hole_every_other_gate_stays_silent(tmp_path: Path) -> None:
    """前面五道闸门一道都不响 —— 这条路上没有任何别的痕迹。"""
    root = _repo(tmp_path)
    before_cfg = git_config(root)
    before_attrs = info_attributes(root)
    before_skip = index_skipped(root)
    _worker_commits_backdoor(root)

    _, paths = capture_diff(root)
    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert diff_suppressed(root, paths) == ()
    assert newly_skipped(before_skip, index_skipped(root)) == ()
    assert changed_config(before_cfg, git_config(root)) == ()
    assert info_attributes(root) == before_attrs


def test_the_hole_git_status_shows_nothing_either(tmp_path: Path) -> None:
    """连 git status 都只报那处干净改动 —— 提交掉的东西不是「改动」了。"""
    root = _repo(tmp_path)
    _worker_commits_backdoor(root)

    status = _git(root, "status", "--porcelain")

    assert "a.py" not in status, f"status 里还有 a.py？{status!r}"
    assert "README.md" in status


# --- 二、分支切换那一支：哈希不变，diff 诚实，但货落别处 -------------------


def test_branch_switch_alone_does_not_blind_the_diff(tmp_path: Path) -> None:
    """只切分支不提交：哈希不变，pdb 照样在 diff 里。

    这条单独看不是「瞎掉监工」，所以它不能靠 diff 的诚实度来判 —— 判据必须
    带上 ref，否则这一支整个漏掉。
    """
    root = _repo(tmp_path)
    h0 = head_commit(root)
    _git(root, "checkout", "-q", "-b", "worker-side")
    (root / "a.py").write_text("x = 1\n" + BACKDOOR, encoding="utf-8")

    diff, paths = capture_diff(root)

    assert head_commit(root) == h0, "切分支不该改哈希"
    assert "pdb.set_trace" in diff, "这一支 diff 是诚实的"
    assert paths == ("a.py",)


def test_branch_switch_moves_the_ref_only(tmp_path: Path) -> None:
    """(哈希, ref) 里只有 ref 变 —— 这正是必须判对而不是只判哈希的原因。"""
    root = _repo(tmp_path)
    before = head_position(root)
    _git(root, "checkout", "-q", "-b", "worker-side")

    after = head_position(root)

    assert after[0] == before[0], "哈希没变"
    assert (before[1], after[1]) == ("refs/heads/feat", "refs/heads/worker-side")
    assert after != before, "判 (哈希, ref) 才拦得住这一支"


# --- 三、head_position 自己的行为 -----------------------------------------


def test_head_position_reports_branch_and_hash(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    h, ref = head_position(root)

    assert h == head_commit(root)
    assert ref == "refs/heads/feat"


def test_head_position_treats_detached_as_empty_ref(tmp_path: Path) -> None:
    """detached 是合法状态（symbolic-ref 在那里以 128 退出），不是错误。"""
    root = _repo(tmp_path)
    _git(root, "checkout", "-q", "--detach")

    h, ref = head_position(root)

    assert h == head_commit(root), "detached 时哈希照样读得到"
    assert ref == ""


def test_head_position_catches_detaching(tmp_path: Path) -> None:
    """挂在分支上 → detached 也是这一轮动了 HEAD，两个方向都得算差异。"""
    root = _repo(tmp_path)
    before = head_position(root)
    _git(root, "checkout", "-q", "--detach")

    assert head_position(root) != before


def test_head_position_catches_a_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = head_position(root)
    _worker_commits_backdoor(root)

    after = head_position(root)

    assert after[0] != before[0], "提交改哈希"
    assert after[1] == before[1], "分支没变 —— 只判 ref 的版本会漏掉这一支"
    assert after != before


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, move: str = ""):
    """跑一轮真 dispatcher。`move` 模拟 worker 在派发**期间**动 HEAD。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class HeadMoving(FakeAdapter):
        # 只动第一轮。既是真实形状（worker 提交一次就够了），也绕开一个
        # 测试自身的坑：闸门判红会打回，adapter 第二轮再跑时已经没有改动
        # 可提交，`git commit` 以 rc=1「无文件要提交」退出，check=True 的
        # helper 当场抛异常 —— 那个失败长得像闸门坏了，其实是闸门生效了。
        moved = False

        def run(self, task, workspace, limits, *, model=None):
            root = Path(workspace)
            if not HeadMoving.moved:
                HeadMoving.moved = True
                if move == "commit":
                    _git(root, "add", "a.py")
                    _git(root, "commit", "-qm", "worker 自己提交")
                elif move == "branch":
                    _git(root, "checkout", "-q", "-b", "worker-side")
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-head",
        prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=HeadMoving([_result(paths=("a.py",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    """dispatcher 真跑的形状：worktree 里干活。顺手留两处脏改动。"""
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    ws = tmp_path / "wt"
    (ws / "a.py").write_text("x = 1\n" + BACKDOOR, encoding="utf-8")
    (ws / "README.md").write_text("hi\nchanged\n", encoding="utf-8")
    return ws


def test_committing_mid_round_blocks_the_merge(tmp_path: Path) -> None:
    rep = _dispatch(tmp_path, _worktree(tmp_path), [], move="commit")
    assert rep.outcome.value != "merged", f"worker 自己提交了却合并：{rep}"


def test_switching_branch_mid_round_blocks_the_merge(tmp_path: Path) -> None:
    """只切分支那一支也得拦 —— 哈希没变，但 land 会提交到别的分支上。"""
    rep = _dispatch(tmp_path, _worktree(tmp_path), [], move="branch")
    assert rep.outcome.value != "merged", f"分支被换了却合并：{rep}"


def test_the_claim_names_the_gate_and_both_positions(tmp_path: Path) -> None:
    """打回的话得说清 HEAD 从哪到哪，worker 才知道要 reset 回去。"""
    rep = _dispatch(tmp_path, _worktree(tmp_path), [], move="commit")
    assert "head-moved" in rep.escalation_reason
    assert "refs/heads/side" in rep.escalation_reason


def test_checks_do_not_run_when_head_moved(tmp_path: Path) -> None:
    """拦在跑 check 之前：监工的视野已经被截短，那份绿不值钱。"""
    ran: list = []
    _dispatch(tmp_path, _worktree(tmp_path), ran, move="commit")
    assert ran == [], f"视野已被截短还去跑 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """不动 HEAD 的正常任务照常合并。这条是这道闸门的误报测试。"""
    rep = _dispatch(tmp_path, _worktree(tmp_path), [], move="")
    assert rep.outcome.value == "merged", f"干净一轮被拦了：{rep}"


# --- 四、基线的位置：必须在轮次循环外 --------------------------------------


def test_moving_head_in_round_one_stays_red_in_round_two(tmp_path: Path) -> None:
    """worker 只在第一轮动 HEAD，之后不再动 —— 仍然不许合并。

    这条钉的是基线**取在哪**，不是判据本身。把 `head_before` 挪到轮次循环
    里面（每轮重取），第一轮照样拦得住 —— 那次移动发生在这一轮之内。但第二轮
    开头重取时 HEAD 已经在移动后的位置上，差异为空，于是判绿合并，而后门还在
    历史里。第五个洞（hooks 基线）就是这个形状，M76a/M76b 各验了一次。

    所以这条测试必须跑到第二轮才有意义：单轮任务对这个错误完全免疫。
    """
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], move="commit")

    assert rep.outcome.value != "merged", (
        f"第二轮把第一轮的移动当成了「本来就这样」：{rep.outcome} / "
        f"{rep.escalation_reason}"
    )
    assert rep.rounds >= 2, f"没跑到第二轮，这条测试没验到基线位置：{rep.rounds}"


def test_head_stays_put_across_a_clean_round(tmp_path: Path) -> None:
    """正常一轮里 HEAD 不动 —— 这道闸门的基线不存在合法漂移。

    land 是全流程唯一的提交点，且跑在这道闸门**之后**；被打回的轮次刻意
    不提交。所以基线取一次就永远对得上。
    """
    ws = _worktree(tmp_path)
    before = head_position(ws)
    rep = _dispatch(tmp_path, ws, [], move="")

    assert rep.outcome.value == "merged"
    after_commit, after_ref = head_position(ws)
    assert after_ref == before[1], "land 换了分支？"
    assert after_commit != before[0], "合并了却没提交？那 land 这步白跑了"


def test_this_repo_has_a_stable_head(tmp_path: Path) -> None:
    """本仓库上连读两次一致 —— 一道在自己仓库上必然误报的闸门等于关掉的闸门。"""
    assert head_position(Path.cwd()) == head_position(Path.cwd())
