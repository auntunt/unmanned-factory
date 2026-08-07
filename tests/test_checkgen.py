"""检查提议器：探针的三种裁决、摘要的边界、以及接进 prd 之后的行为。

这些测试全部不打模型 —— probe_check 跑的是真命令（用 tmp_path 里的小仓库），
CheckProposer 的模型调用用假二进制替掉。红前绿后那条不变式恰好是**能**
离线测的：它只依赖「在未改动的仓库里跑一遍」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.intake.checkgen import (
    CheckGenError,
    CheckProposer,
    Probe,
    Proposal,
    _looks_broken,
    probe_check,
    repo_digest,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个 slugify **不存在**的仓库 —— 探针要在这种状态下跑。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "text.py").write_text("def upper(s):\n    return s.upper()\n",
                                              encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.12"\n', encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- 三种裁决

def test_a_check_that_fails_for_the_right_reason_is_kept(repo):
    p = probe_check({"name": "real", "command":
                     'python3 -c "from src.text import slugify"'}, repo)
    assert p.verdict == "fail_missing"
    assert p.usable


def test_a_check_that_already_passes_is_discarded(repo):
    # 功能还没写就绿 —— 它证明不了改动做成了，留下等于白花一轮。
    p = probe_check({"name": "vacuous", "command": "true"}, repo)
    assert p.verdict == "already_green"
    assert not p.usable


def test_checking_only_that_a_file_exists_is_caught_as_vacuous(repo):
    # 提示词里点名禁掉的那种假验收。静态看不出来，跑一下立刻退出 0。
    p = probe_check({"command": "test -f src/text.py"}, repo)
    assert p.verdict == "already_green"


def test_an_assertionless_script_is_caught_as_vacuous(repo):
    p = probe_check({"command": 'python3 -c "pass"'}, repo)
    assert p.verdict == "already_green"


def test_a_typo_in_the_interpreter_is_broken_not_a_real_failure(repo):
    # 127。改完功能它还是红的，会连烧三轮只报「非零退出」。
    p = probe_check({"command": 'pyhton3 -c "print(1)"'}, repo)
    assert p.verdict == "broken"
    assert p.exit_code == 127


def test_a_shell_syntax_error_is_broken(repo):
    p = probe_check({"command": 'python3 -c "print(1)'}, repo)
    assert p.verdict == "broken"


def test_an_empty_command_is_broken_without_running_anything(repo):
    p = probe_check({"command": "   "}, repo)
    assert p.verdict == "broken"
    assert p.exit_code is None


def test_a_hanging_check_is_discarded_rather_than_kept(repo):
    # 探针超时的 check 在回归监工那里同样会超时，留下只是把卡点往后挪。
    p = probe_check({"command": "sleep 5"}, repo, timeout_s=1)
    assert p.verdict == "broken"
    assert "超时" in p.detail


def test_stdout_contains_is_judged_on_output_not_exit_code(repo):
    # 命令退出 0，但要的字符串不在 stdout 里 → 还是红的，留下。
    p = probe_check({"command": 'echo hello', "expect": "stdout_contains",
                     "value": "world"}, repo)
    assert p.verdict == "fail_missing"

    q = probe_check({"command": 'echo world', "expect": "stdout_contains",
                     "value": "world"}, repo)
    assert q.verdict == "already_green"


# ---------------------------------------------------- 退出码 2 的歧义

def test_exit_two_alone_is_not_treated_as_broken(repo):
    # pytest 收集失败也是 2，而收集失败常常正是「功能还没写」。
    # 只按退出码判会把好 check 扔掉，所以 2 必须靠 stderr 措辞再看一眼。
    assert not _looks_broken(2, "", "")


def test_exit_two_with_a_shell_complaint_is_broken():
    assert _looks_broken(2, "sh: syntax error: unexpected end of file", "")


def test_command_not_found_in_stderr_is_broken_whatever_the_code():
    assert _looks_broken(1, "bash: line 1: fooo: command not found", "")


def test_a_plain_assertion_failure_is_not_broken():
    assert not _looks_broken(1, "AssertionError: expected hello-world", "")


def test_a_missing_module_is_not_broken_it_is_the_thing_being_verified(repo):
    # ImportError 是「还没写」的标准形态。判成 broken 就没有任何 check 能留下了。
    p = probe_check({"command": 'python3 -c "import nope_not_here"'}, repo)
    assert p.verdict == "fail_missing"


# ---------------------------------------------------------- 仓库摘要

def test_the_digest_names_the_build_config(repo):
    d = repo_digest(repo)
    assert "pyproject.toml" in d
    assert "requires-python" in d


def test_the_digest_lists_paths_but_no_file_contents(repo):
    # 少递一样东西就少一条它把仓库现成代码抄进 command 的路。
    d = repo_digest(repo)
    assert "src/text.py" in d
    assert "def upper" not in d
    assert "s.upper()" not in d


def test_the_digest_skips_git_and_caches(repo):
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
    d = repo_digest(repo)
    assert ".git" not in d
    assert "__pycache__" not in d


def test_the_digest_is_deterministic(repo):
    # 递给模型的输入必须可复现，否则同一份需求两次跑出两套 checks，
    # 「为什么这次少了一条」就不可查。
    assert repo_digest(repo) == repo_digest(repo)


def test_the_digest_truncates_instead_of_growing_without_bound(repo):
    for i in range(200):
        (repo / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    d = repo_digest(repo, max_entries=10)
    assert "（截断）" in d
    assert "f199.txt" not in d


def test_an_empty_repo_still_produces_a_digest(tmp_path):
    d = repo_digest(tmp_path)
    assert "（没有）" in d
    assert "（空仓库）" in d


# ------------------------------------------------------- 提议器：探针之后的取舍

def _proposer(payload: dict) -> CheckProposer:
    """把模型调用换成固定 payload，只测探针之后的取舍逻辑。"""
    p = CheckProposer(probe_timeout_s=10)
    p._ask = lambda prompt: dict(payload)  # type: ignore[method-assign]
    return p


def test_no_acceptance_means_no_proposal_and_no_model_call(repo):
    called = []
    p = CheckProposer()
    p._ask = lambda prompt: called.append(prompt) or {}  # type: ignore[method-assign]
    out = p.propose(acceptance=(), prompt="随便", workspace=repo)
    assert out.checks == ()
    # 没有验收标准时无从反推，所以连钱都不花。这种草稿闸门本来就会拦。
    assert called == []


def test_blank_acceptance_entries_do_not_count_as_criteria(repo):
    p = CheckProposer()
    p._ask = lambda prompt: pytest.fail("不该调用模型")  # type: ignore[method-assign]
    assert p.propose(acceptance=("", "   ", "\n"), prompt="x", workspace=repo).checks == ()


def test_a_missing_workspace_raises_instead_of_guessing(tmp_path):
    p = _proposer({"checks": []})
    with pytest.raises(CheckGenError, match="仓库路径不存在"):
        p.propose(acceptance=("能跑",), prompt="x", workspace=tmp_path / "nope")


def test_only_the_checks_that_fail_for_the_right_reason_survive(repo):
    out = _proposer({"checks": [
        # 导入模块本身不够 —— src.text 已经存在，那条会当场探成 already_green。
        # 要探的是**还没写的那个函数**。
        {"name": "real", "command": "python3 -c 'from src.text import slugify'",
         "covers": "a"},
        {"name": "vacuous", "command": "true", "covers": "a"},
        {"name": "typo", "command": "pyhton3 -c 'x'", "covers": "a"},
    ]}).propose(acceptance=("a",), prompt="x", workspace=repo)

    assert [c["name"] for c in out.checks] == ["real"]
    # 扔掉的留在 rejected 里：只报留下的会让「为什么只有一条」变成一次排查。
    assert {p.verdict for p in out.rejected} == {"already_green", "broken"}


def test_a_candidate_without_a_command_is_dropped_before_the_probe(repo):
    out = _proposer({"checks": [
        {"name": "empty", "covers": "a"},
        {"name": "real", "command": "false", "covers": "a"},
    ]}).propose(acceptance=("a",), prompt="x", workspace=repo)
    assert [c["name"] for c in out.checks] == ["real"]
    assert out.rejected == ()


def test_covers_is_not_carried_into_the_task_yaml(repo):
    out = _proposer({"checks": [
        {"name": "n", "command": "false", "covers": "抄的原文", "expect": "exit_zero"},
    ]}).propose(acceptance=("a",), prompt="x", workspace=repo)
    # covers 只是让模型逐条对齐验收标准，Task 的 check schema 里没这个字段。
    assert set(out.checks[0]) == {"name", "command", "expect"}


def test_a_nameless_check_still_gets_a_name(repo):
    out = _proposer({"checks": [{"command": "false"}]}).propose(
        acceptance=("a",), prompt="x", workspace=repo)
    assert out.checks[0]["name"] == "check"


def test_uncheckable_criteria_come_back_instead_of_being_faked(repo):
    out = _proposer({"checks": [], "uncheckable": ["界面要好看"]}).propose(
        acceptance=("界面要好看",), prompt="x", workspace=repo)
    assert out.checks == ()
    assert out.uncheckable == ("界面要好看",)


def test_cost_and_tokens_are_reported(repo):
    out = _proposer({"checks": [], "_tokens": 8660, "_cost": 0.0312}).propose(
        acceptance=("a",), prompt="x", workspace=repo)
    assert (out.tokens, out.cost_usd) == (8660, 0.0312)


def test_the_summary_names_every_discarded_candidate_and_why(repo):
    out = _proposer({"checks": [
        {"name": "vacuous", "command": "true"},
        {"name": "real", "command": "false"},
    ], "uncheckable": ["手感要顺"]}).propose(
        acceptance=("a",), prompt="x", workspace=repo)

    blob = "\n".join(out.lines())
    assert "提议 2 条，探针留下 1 条" in blob
    assert "vacuous" in blob and "already_green" in blob
    assert "手感要顺" in blob


# --------------------------------------------------------- 接进 prd：失败要 fail-closed
#
# 提议这一步在闸门**之前**跑（闸门第一条硬拦截就是「没有可执行的 check」）。
# 所以它失败的后果必须是「草稿带着空 checks 进闸门 → 落 needs-human 等人」，
# 也就是退回没有这个功能之前的状态，而不是抛异常把整条 prd 打断。

import argparse   # noqa: E402

from factory.cli import _propose_checks   # noqa: E402
from factory.intake import checkgen as _cg   # noqa: E402
from factory.intake.extract import DraftTask   # noqa: E402


def _draft(**kw) -> DraftTask:
    base = dict(task_id="T-x", prompt="加个 slugify",
                acceptance=("slugify('A B') == 'a-b'",))
    base.update(kw)
    return DraftTask(**base)


def _ns(workspace, **kw) -> argparse.Namespace:
    base = dict(workspace=str(workspace) if workspace else None,
                binary="claude", intake_model="sonnet", propose_checks=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_surviving_checks_are_merged_into_the_draft(repo, monkeypatch):
    class Fake:
        def __init__(self, **kw): pass
        def propose(self, **kw):
            return Proposal(checks=({"name": "n", "command": "false"},),
                            tokens=100, cost_usd=0.03)

    monkeypatch.setattr(_cg, "CheckProposer", Fake)
    out = _propose_checks(_draft(tokens=7, cost_usd=0.01), _ns(repo))
    assert out.checks == ({"name": "n", "command": "false"},)
    # 提议这一步的钱是这张草稿花的。不累加会让入口层成本少算。
    assert out.tokens == 107
    assert out.cost_usd == pytest.approx(0.04)


def test_a_failed_proposal_returns_the_draft_unchanged(repo, monkeypatch):
    class Boom:
        def __init__(self, **kw): pass
        def propose(self, **kw): raise CheckGenError("模型调用失败：overloaded")

    monkeypatch.setattr(_cg, "CheckProposer", Boom)
    draft = _draft()
    out = _propose_checks(draft, _ns(repo))
    # 不抛。空 checks 的草稿到闸门那里会被拦进 needs-human —— 那正是想要的降级。
    assert out is draft
    assert out.checks == ()


def test_no_workspace_skips_the_proposal_instead_of_probing_nothing(monkeypatch):
    monkeypatch.setattr(_cg, "CheckProposer",
                        lambda **kw: pytest.fail("不该构造提议器"))
    draft = _draft()
    assert _propose_checks(draft, _ns(None)) is draft


def test_a_proposal_that_survives_nothing_leaves_checks_empty(repo, monkeypatch):
    class Empty:
        def __init__(self, **kw): pass
        def propose(self, **kw):
            return Proposal(rejected=(Probe({"name": "v"}, "already_green", 0),))

    monkeypatch.setattr(_cg, "CheckProposer", Empty)
    out = _propose_checks(_draft(), _ns(repo))
    assert out.checks == ()
