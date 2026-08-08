"""`.git/info/attributes` —— 第三条设 attributes 的路，前两道闸门都管不到。

洞的形状：attributes 一共能从三个地方设。tree 里的 `.gitattributes` 被第七道
闸门（diff_suppressed）管着，`core.attributesFile` 被第十三道（git-config-touched）
管着，而 `.git/info/attributes` 两道都漏。

为什么第七道漏它：那道的基线是 `git check-attr --source=HEAD`，而 `--source`
只换「tree 里那份 .gitattributes」。info/attributes 是仓库级的，基线侧和现状侧
**都会读它** → 差异恒为空。

危害面比第七个洞大：`.git/info` 在 common dir，父仓库和所有并行 worktree 共用。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.harness.workspace import (
    capture_diff,
    changed_config,
    diff_suppressed,
    git_config,
    index_skipped,
    info_attributes,
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
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _info_attrs(root: Path) -> Path:
    d = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (d / "info").mkdir(parents=True, exist_ok=True)
    return d / "info" / "attributes"


def _dirty(root: Path) -> None:
    (root / "a.py").write_text("x = 1\nimport pdb; pdb.set_trace()\n", encoding="utf-8")


# --- 一、先证明洞是真的 ---------------------------------------------------


def test_the_hole_info_attributes_degrades_the_diff(tmp_path: Path) -> None:
    """一行 `*.py -diff` 写进 .git/info/attributes，监工看不到那行 pdb。"""
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")
    _dirty(root)

    diff, paths = capture_diff(root)

    assert "pdb.set_trace" not in diff, "洞不存在了？那这个检测可以删"
    assert "Binary files" in diff
    assert paths == ("a.py",), "路径侧不受影响 —— 范围监工照常工作"


def test_the_hole_slips_past_gate_seven(tmp_path: Path) -> None:
    """第七道闸门（diff_suppressed）对它完全静默。

    这是这个洞的要点：`--source=HEAD` 只换 tree 里那份 .gitattributes，
    info/attributes 在基线侧和现状侧都被读到 → 差异恒为空。
    """
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")
    _dirty(root)

    diff, paths = capture_diff(root)
    assert "pdb.set_trace" not in diff, "前提没成立：diff 没被关掉"
    assert diff_suppressed(root, paths) == (), "第七道闸门抓到了？那这个洞不存在"


def test_the_hole_slips_past_every_other_gate(tmp_path: Path) -> None:
    """#12 / #13 / 影子代码 / runner hook 也都看不见它。"""
    root = _repo(tmp_path)
    before_cfg = git_config(root)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")
    _dirty(root)

    assert changed_config(before_cfg, git_config(root)) == ()
    assert index_skipped(root) == frozenset()
    assert shadow_code(root) == ()
    assert runner_hooks(root) == ()


def test_the_file_itself_is_invisible_to_git(tmp_path: Path) -> None:
    """写它这个动作本身不留痕：不在 diff 里，也不在 git status 里。

    所有「查 diff 里有没有可疑改动」的检测对它天然免疫 —— 它在 .git/ 下面，
    git 不跟踪自己的元数据目录。
    """
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")

    diff, paths = capture_diff(root)
    assert diff == "" and paths == (), f"居然进了 diff：{paths}"
    assert _git(root, "status", "--porcelain") == ""


def test_the_blast_radius_is_every_parallel_worktree(tmp_path: Path) -> None:
    """从一个 worktree 写进去，父仓库的 diff 也瞎掉。

    `.git/info` 在 common dir。这是它比第七个洞更严重的地方：第七个洞的
    `.gitattributes` 在树里，只影响那一个 worktree；这个文件是共用的，
    一个 worker 能污染所有并行任务的监工。
    """
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"

    _info_attrs(ws).write_text("*.py -diff\n", encoding="utf-8")
    _dirty(root)
    _dirty(ws)

    assert "pdb.set_trace" not in capture_diff(ws)[0], "worktree 侧没生效？"
    assert "pdb.set_trace" not in capture_diff(root)[0], "父仓库没被波及 —— 危害面判错了"


# --- 二、判据本身 ---------------------------------------------------------


def test_a_missing_file_is_the_empty_string(tmp_path: Path) -> None:
    """文件不存在时给空串，不抛。绝大多数仓库没有这个文件（本仓库就没有）。"""
    assert info_attributes(_repo(tmp_path)) == ""


def test_writing_the_file_changes_the_fingerprint(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = info_attributes(root)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")
    assert info_attributes(root) != before


def test_editing_the_file_changes_the_fingerprint(tmp_path: Path) -> None:
    """判的是内容，不是「文件在不在」—— 仓库本来有这个文件时才有意义。"""
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.lock -diff\n", encoding="utf-8")
    before = info_attributes(root)
    _info_attrs(root).write_text("*.lock -diff\n*.py -diff\n", encoding="utf-8")
    assert info_attributes(root) != before


def test_deleting_the_file_changes_the_fingerprint(tmp_path: Path) -> None:
    """删也要报，和 changed_hooks 同一个理由：那可能是人设的一道东西。"""
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.lock -diff\n", encoding="utf-8")
    before = info_attributes(root)
    _info_attrs(root).unlink()
    assert info_attributes(root) != before


def test_a_pre_existing_file_is_stable_across_a_round(tmp_path: Path) -> None:
    """仓库本来有它、这一轮没人动 → 指纹不变。这是零误报的判据。"""
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.lock -diff\n", encoding="utf-8")
    before = info_attributes(root)
    _dirty(root)
    capture_diff(root)
    assert info_attributes(root) == before


def test_a_worktree_reads_the_shared_file(tmp_path: Path) -> None:
    """worktree 里的 `.git` 是文件不是目录 —— 不能拼路径，得问 git。"""
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.py -diff\n", encoding="utf-8")
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    assert info_attributes(tmp_path / "wt") == info_attributes(root) != ""


def test_a_non_repo_is_empty_not_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert info_attributes(plain) == ""


def test_this_repo_has_no_info_attributes(tmp_path: Path) -> None:
    """本仓库的基线是空串。这条是误报面的度量：不是空的话这道闸门会天天响。"""
    assert info_attributes(Path.cwd()) == ""


# --- 三、接线：真跑一轮 dispatcher ----------------------------------------


def _dispatch(tmp_path: Path, ws: Path, checks_ran: list, *, attrs: str | None = None):
    """跑一轮真 dispatcher。`attrs` 模拟 worker 在派发**期间**写 info/attributes。"""
    from factory.audit.models import SupervisorRole, Verdict
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from factory.supervisors.base import SupervisorReport
    from factory.task import CheckSpec, Task
    from tests.test_dispatcher import FakeAdapter, _result

    class AttrWriting(FakeAdapter):
        def run(self, task, workspace, limits, *, model=None):
            if attrs is not None:
                _info_attrs(Path(workspace)).write_text(attrs, encoding="utf-8")
            return super().run(task, workspace, limits, model=model)

    class Recording:
        role = SupervisorRole.REGRESSION

        def review(self, workspace, checks):
            checks_ran.append(tuple(c.name for c in checks))
            return SupervisorReport(role=self.role, verdict=Verdict.PASS)

    task = Task(
        task_id="T-ia",
        prompt="改点东西",
        checks=(CheckSpec(name="ok", command="true"),),
    )
    d = Dispatcher(
        adapter=AttrWriting([_result(paths=("a.py",))]),
        store=AuditStore(tmp_path / "a.db"),
        supervisor=Recording(),
    )
    return d.run(task, ws)


def _worktree(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    _dirty(ws)
    return ws


def test_touching_info_attributes_blocks_the_merge(tmp_path: Path) -> None:
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    assert rep.outcome.value != "merged", f"写了 info/attributes 却合并了：{rep}"


def test_the_claim_says_what_to_undo(tmp_path: Path) -> None:
    """打回的话里得说是哪个文件，worker 才知道撤什么。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    assert "info/attributes" in rep.escalation_reason
    assert "info-attributes-touched" in rep.escalation_reason


def test_checks_do_not_run_when_attributes_changed(tmp_path: Path) -> None:
    """拦在跑 check 之前 —— 监工的输入已经不可信，那份绿没有意义。"""
    ws = _worktree(tmp_path)
    ran: list = []
    _dispatch(tmp_path, ws, ran, attrs="*.py -diff\n")
    assert ran == [], f"输入不可信还去跑 check：{ran}"


def test_a_clean_round_still_merges(tmp_path: Path) -> None:
    """不动这个文件的正常任务照常合并。这条是这道闸门的误报测试。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs=None)
    assert rep.outcome.value == "merged", f"干净一轮被拦了：{rep}"


def test_a_pre_existing_file_is_not_flagged_by_the_gate(tmp_path: Path) -> None:
    """仓库**本来就有** info/attributes、这一轮没人动 → 照常合并。

    刻意测**闸门**而不只测 info_attributes()：把闸门改成「文件存在就报」
    的话，前者照样通过、后者会红。本会话 M83 就是这么活下来的。
    """
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.lock -diff\n", encoding="utf-8")
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    _dirty(ws)

    rep = _dispatch(tmp_path, ws, [], attrs=None)
    assert rep.outcome.value == "merged", f"合法的既有文件被判红了：{rep}"


def test_appending_to_a_pre_existing_file_blocks_the_merge(tmp_path: Path) -> None:
    """仓库本来就有 info/attributes，worker 往里**追加**一行 → 必须判红。

    这条测试是变异 M111 逼出来的。M111 把闸门的判据从

        if info_attributes(workspace) != attrs_before:

    换成

        if info_attributes(workspace) and not attrs_before:

    也就是「从无到有才算」。离线全套 929 条**一条没红**：这道闸门的每一条
    接线测试都从「仓库里没有这个文件」起跑，于是「变了」和「从无到有」给出
    同一个答案。而上面那条误报测试恰好证明了空基线不是唯一合法形态 ——
    仓库带一份 info/attributes 是正常的（本机不该提交的属性配置就该放那儿），
    在那种仓库上被降级的判据完全哑掉，worker 追加一行 `*.py -diff` 就通关。

    和 M108（refs/replace 只判条数变多）是同一个病：**基线为空的接线测试对
    判据的形状几乎没有约束力**。修的是测试，不是代码 —— 出厂的 `!=` 一直是对的。
    """
    root = _repo(tmp_path)
    _info_attrs(root).write_text("*.lock -diff\n", encoding="utf-8")
    _git(root, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    ws = tmp_path / "wt"
    _dirty(ws)
    before = info_attributes(ws)
    assert before, "前提：派发前基线非空，否则这条测试退化成又一条空基线用例"

    # 保留原有那行，只追加 —— 「从无到有」不成立，但内容变了。
    rep = _dispatch(tmp_path, ws, [], attrs="*.lock -diff\n*.py -diff\n")

    assert info_attributes(ws) != before, "前提：内容真的变了"
    assert rep.outcome.value != "merged", "往既有文件里追加过了 —— 判据只看了有无"
    assert "info-attributes-touched" in rep.escalation_reason


def test_the_other_gates_stay_quiet(tmp_path: Path) -> None:
    """只有这一条 claim 响。糊到别的闸门上会让打回的指令指错方向。"""
    ws = _worktree(tmp_path)
    rep = _dispatch(tmp_path, ws, [], attrs="*.py -diff\n")
    reasons = rep.escalation_reason
    assert "info-attributes-touched" in reasons
    for other in ("diff-suppressed", "git-config-touched", "index-skip-flag",
                  "shadow-code", "runner-hook-added", "git-hook-touched"):
        assert other not in reasons, f"{other} 也响了"
