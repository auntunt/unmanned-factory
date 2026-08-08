"""worker 改掉审它自己的那份验收标准。

洞的形状：`_review` 里 `criteria=task.criteria(workspace)` —— 每轮都重新从
**仓库里那份 spec_doc** 解析。那个文件在 sandbox 的可写范围内。于是被审
对象能改改判它的标准。和第五个洞（hook 基线在循环内重取）同一个形状。

两个方向都实测成立：
  改正文 —— 监工拿着改后的标准核 diff，判 pass。
  删条目 —— resolve_spec().ok 变 False，但 criteria() 不看 ok，静默少一条。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.task import Task

BASE = (
    "- AC-1: slugify 必须把字母小写化\n"
    "- AC-2: 空格必须转成横线\n"
    "- AC-9: 无关章节，属于另一个任务的标准\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _doc_repo(tmp_path: Path, text: str = BASE) -> Path:
    """一个 PRD 已提交的仓库。PRD 要在 HEAD 里，否则 land 会把它一起提交。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "PRD.md").write_text(text, encoding="utf-8")
    (root / "textutil.py").write_text("def slugify(s):\n    return s\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _task(**kw) -> Task:
    return Task(
        task_id=kw.pop("task_id", "T-sm"),
        prompt="实现 slugify",
        spec_ref=kw.pop("spec_ref", ("AC-1", "AC-2")),
        spec_doc=kw.pop("spec_doc", "PRD.md"),
        **kw,
    )


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_rewriting_the_body_swaps_the_criteria(tmp_path: Path) -> None:
    """改正文：递给规格监工的标准整句被换掉。"""
    root = _doc_repo(tmp_path)
    t = _task()
    before = t.criteria(root)
    assert before == ("AC-1: slugify 必须把字母小写化", "AC-2: 空格必须转成横线")

    (root / "PRD.md").write_text(
        BASE.replace("必须把字母小写化", "必须保持原样，此条已由上游确认满足"),
        encoding="utf-8",
    )

    after = t.criteria(root)
    assert after != before
    assert "已由上游确认满足" in after[0], "监工核的就是这句 worker 自己写的话"


def test_the_hole_deleting_an_entry_degrades_silently(tmp_path: Path) -> None:
    """删条目：criteria 从 2 条静默降到 1 条，少的那条没人提起。

    这一侧比改正文更糟 —— `resolve_spec().ok` 明明变成了 False，而
    `criteria()` 刻意不看 ok（它只负责「有什么给什么」），派发路径上
    第二次解析也没人再查一次 ok。
    """
    root = _doc_repo(tmp_path)
    t = _task()
    assert len(t.criteria(root)) == 2

    (root / "PRD.md").write_text("- AC-1: slugify 必须把字母小写化\n", encoding="utf-8")

    resolved = t.resolve_spec(root)
    assert resolved.ok is False and resolved.missing == ("AC-2",)
    assert len(t.criteria(root)) == 1, "少了一条，而且没有任何人报错"


def test_the_hole_is_invisible_to_the_other_gates(tmp_path: Path) -> None:
    """改 PRD.md 这个动作，前四道闸门全看不见。

    它是一个已跟踪的普通文本文件：不被 .gitignore 挡（shadow_code 空）、
    不是 runner 会加载的配置（runner_hooks 空）、diff 正文照常可见
    （diff_suppressed 空）、也不是 git hook。
    """
    from factory.harness.workspace import (
        capture_diff,
        diff_suppressed,
        runner_hooks,
        shadow_code,
    )

    root = _doc_repo(tmp_path)
    (root / "PRD.md").write_text(BASE.replace("小写化", "保持原样"), encoding="utf-8")

    diff, paths = capture_diff(root)
    assert "PRD.md" in paths, "改动本身是看得见的 —— 看不见的是它改了判据"
    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()
    assert diff_suppressed(root, tuple(paths)) == ()


# --- 二、判据：只覆盖本任务自己的那几条 -----------------------------------


def test_editing_someone_elses_entry_is_not_flagged(tmp_path: Path) -> None:
    """改文档里**别的编号**不响。一个仓库的 PRD 里有别的任务的章节。"""
    root = _doc_repo(tmp_path)
    t = _task()
    before = t.criteria(root)
    (root / "PRD.md").write_text(
        BASE.replace("属于另一个任务的标准", "改写过的、别人的标准"), encoding="utf-8"
    )
    assert t.criteria(root) == before


def test_appending_a_new_entry_is_not_flagged(tmp_path: Path) -> None:
    """往文档尾部追加新条目不响。写文档是合法工作。"""
    root = _doc_repo(tmp_path)
    t = _task()
    before = t.criteria(root)
    (root / "PRD.md").write_text(BASE + "- AC-10: 后来补的一条\n", encoding="utf-8")
    assert t.criteria(root) == before


def test_deleting_the_whole_doc_is_flagged(tmp_path: Path) -> None:
    """整份文档删掉 → criteria 空 → 和基线不同。

    这条单列是因为它走的是 doc_error 而不是 missing，别的路径。
    """
    root = _doc_repo(tmp_path)
    t = _task()
    before = t.criteria(root)
    (root / "PRD.md").unlink()
    assert t.criteria(root) == () != before


def test_a_task_without_a_spec_doc_never_trips_this_gate(tmp_path: Path) -> None:
    """口述来源的任务 criteria 恒定 —— 这道闸门对它们永远静默。

    这是必须验的：绝大多数任务是口述来源的（见范围监工 declared_paths
    为空的同一个理由）。一道在多数任务上误报的闸门等于一道被关掉的闸门。
    """
    root = _doc_repo(tmp_path)
    t = Task(
        task_id="T-oral", prompt="随便改", acceptance=("函数要能跑", "别删测试")
    )
    before = t.criteria(root)
    (root / "PRD.md").write_text("整份换掉\n", encoding="utf-8")
    (root / "PRD.md").unlink()
    assert t.criteria(root) == before == ("函数要能跑", "别删测试")


# --- 三、打回的话说得清不清 -----------------------------------------------


def test_delta_lists_what_is_gone_not_what_is_new(tmp_path: Path) -> None:
    """只列丢了哪几条，不列新出现的。

    改一条正文会同时产生一进一出。把「新出现的」也摊给 worker，它看到的
    是自己写的那句话被系统引用一遍，容易读成「已被采纳」。
    """
    from factory.dispatcher import _criteria_delta

    gone = _criteria_delta(("AC-1: 小写化", "AC-2: 转横线"), ("AC-1: 保持原样", "AC-2: 转横线"))
    assert gone == ("AC-1: 小写化",)
    assert not any("保持原样" in g for g in gone)


def test_delta_never_returns_empty(tmp_path: Path) -> None:
    """条数和内容都没丢、但顺序被换了这种情形，也得给出一句话。

    调用方只在 before != after 时才进来，所以空返回等于一条 got 里什么都
    不说的 claim。宁可给一句笼统的，也不给空。
    """
    from factory.dispatcher import _criteria_delta

    assert _criteria_delta(("a", "b"), ("b", "a")) == ("（条数没变，正文被改写）",)


# --- 四、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, prd: str | None = None):
    """跑一轮真 dispatcher。`prd` 模拟 worker 在派发**期间**改 PRD.md。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec
    from tests.test_dispatcher import FakeAdapter, _result

    class PrdWriting(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if prd is not None:
                (Path(workspace) / "PRD.md").write_text(prd, encoding="utf-8")
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = _task(checks=(CheckSpec(name="ok", command="true"),))
    d = Dispatcher(
        adapter=PrdWriting([_result(paths=("textutil.py", "PRD.md"))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    root = _doc_repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    (ws / "textutil.py").write_text(
        "def slugify(s):\n    return s.lower().replace(' ', '-')\n", encoding="utf-8"
    )
    return ws


def test_rewriting_the_criteria_blocks_the_merge(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(
        tmp_path, ws, [], prd=BASE.replace("必须把字母小写化", "已由上游确认满足")
    )
    assert rep.outcome.value != "merged", f"改了判据却合并了：{rep}"


def test_deleting_a_criterion_blocks_the_merge(tmp_path: Path) -> None:
    """删条目那一侧也拦得住 —— 这一侧原来是静默通过的。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], prd="- AC-1: slugify 必须把字母小写化\n")
    assert rep.outcome.value != "merged", f"删掉一条标准却合并了：{rep}"


def test_the_claim_names_the_criterion_that_is_gone(tmp_path: Path) -> None:
    """打回的话里得有那条标准的正文，worker 才知道该把什么改回去。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], prd="- AC-1: slugify 必须把字母小写化\n")

    reasons = rep.escalation_reason
    assert "spec-criteria-mutated" in reasons
    assert "空格必须转成横线" in reasons, f"没说丢了哪一条：{reasons[:400]}"
    # 顺手钉一下别的闸门没跟着响 —— 这个洞的四个邻居都该保持静默。
    for other in ("shadow-code", "runner-hook-added", "diff-suppressed"):
        assert other not in reasons


def test_checks_do_not_run_when_the_criteria_moved(tmp_path: Path) -> None:
    """闸门在跑 check 之前 —— 判据都不可信了，那份绿没有意义。"""
    ran: list = []
    ws = _worktree(tmp_path)
    _dispatch(tmp_path, ws, ran, prd="- AC-1: slugify 必须把字母小写化\n")
    assert ran == [], f"判据被改了还去跑 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """不动 PRD 的一轮照常合并。这条钉的是「闸门没把正常路堵死」。"""
    ran: list = []
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, ran, prd=None)
    assert rep.outcome.value == "merged", f"干净的一轮被拦了：{rep}"
    assert ran and ran[0][0] == "ok"


def test_appending_to_the_prd_still_merges(tmp_path: Path) -> None:
    """worker 往 PRD 尾部补一条（合法的文档工作）不该被拦。

    这条和上一条不是重复：上一条是「完全不碰 PRD」，这条是「碰了 PRD 但
    没碰自己那几条」—— 判据窄到不窄，差别就在这里。
    """
    ran: list = []
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, ran, prd=BASE + "- AC-10: 顺手补的一条\n")
    assert rep.outcome.value == "merged", f"改了别人的章节被拦：{rep}"


def test_review_refuses_to_run_without_the_baseline(tmp_path: Path) -> None:
    """`criteria_before` 没有默认值 —— 漏传当场 TypeError。

    给它填 `()` 的话，漏传的调用方在每个引 spec_doc 的任务上都判红
    （空基线 vs 有标准 = 差异）。和 hooks_before 同一个理由。
    """
    import inspect

    from factory.dispatcher import Dispatcher

    sig = inspect.signature(Dispatcher._review)
    p = sig.parameters["criteria_before"]
    assert p.default is inspect.Parameter.empty


def test_the_second_round_still_sees_the_mutation(tmp_path: Path) -> None:
    """第一轮改标准被打回，第二轮**不改回去**照样拦得住。

    这条钉的是「基线取在循环外」，M76 的另一半：把基线改成每轮开头重取的
    版本，第一轮照样判红（那时基线还是原样），第二轮开头重取时改后的文档
    已经在里面了 → 差异为空 → 合并。第五个洞（hook 基线）就是这么漏的，
    而且是 rounds=2 的真跑才抓到。

    所以第一轮判红**不足以**证明基线的位置对。
    """
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec
    from tests.test_dispatcher import FakeAdapter, _result

    ws = _worktree(tmp_path)
    mutated = "- AC-1: slugify 必须把字母小写化\n"

    class OnceWriting(FakeAdapter):
        """第一轮改掉 PRD，第二轮什么都不做（改后的文档留在树上）。"""

        rounds = 0

        def run(self, task, workspace, limits, *, model=None):
            OnceWriting.rounds += 1
            if OnceWriting.rounds == 1:
                (Path(workspace) / "PRD.md").write_text(mutated, encoding="utf-8")
            return super().run(task, workspace, limits, model=model)

    class Passing:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = _task(checks=(CheckSpec(name="ok", command="true"),), max_rounds=2)
    d = Dispatcher(
        adapter=OnceWriting(
            [_result(paths=("textutil.py", "PRD.md")), _result(paths=("textutil.py",))]
        ),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Passing(),
    )
    rep = d.run(task, ws)

    assert OnceWriting.rounds == 2, f"没跑到第二轮，这条测不到东西：{rep}"
    assert rep.outcome.value != "merged", (
        f"第二轮把改后的文档当成了基线 —— 少的那条标准从此没人提起：{rep}"
    )
    assert "spec-criteria-mutated" in rep.escalation_reason
