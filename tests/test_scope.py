"""范围监工测试。

两条最要紧的：

  1. **declared_paths 为空 → PASS。** 抽取器明确要求用户没说文件名就留空，
     所以空是常态。空当成「什么都不许改」会让几乎每个口述任务被打回，
     而且打回理由 worker 修不了 —— 三轮烧完上人。
  2. **目录形式的声明要能匹配目录下的文件。** fnmatch 的 `*` 不跨 `/`，
     写 `docs/` 却匹配不上 `docs/readme.md` 的话，声明了反而全部越界 ——
     这是「加了约束结果更糟」，比没有约束更难查。
"""

from __future__ import annotations

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.scope import ScopeSupervisor, out_of_scope


def review(changed, declared):
    return ScopeSupervisor().review(changed_paths=changed,
                                    declared_paths=declared)


# ── 没有约定的情况 ────────────────────────────────────────────────────────

def test_no_declaration_means_no_contract_so_it_passes():
    """intake/extract.py 要求「用户没说文件名就留空数组」，空是常态不是错误。"""
    rep = review(("a.py", "b.py", "docs/x.md"), ())
    assert rep.verdict == Verdict.PASS and rep.claims == ()


def test_blank_entries_do_not_count_as_a_declaration():
    """YAML 里写了 declared_paths: 然后底下是空的，解析出来是 ('',)。
    把空串当成一条约定，会让所有改动都越界。"""
    assert review(("a.py",), ("", "   ")).verdict == Verdict.PASS


def test_it_reports_pass_rather_than_staying_silent():
    """PASS 也要落一条记录：跳过的话 §5.1 里这个监工看起来从没审过，
    分母是 0，裁剪建议就永远是「数据不足」。"""
    rep = review(("greet.py",), ("greet.py",))
    assert rep.role == SupervisorRole.SCOPE and rep.verdict == Verdict.PASS


# ── 越界 ──────────────────────────────────────────────────────────────────

def test_an_undeclared_file_is_out_of_scope():
    rep = review(("greet.py", "docs/readme.md"), ("greet.py",))
    assert rep.verdict == Verdict.FAIL
    got = rep.claims[0]["got"]
    assert "docs/readme.md" in got and "greet.py" not in got


def test_the_claim_names_the_files_so_the_worker_can_fix_it():
    """claims 是打回 worker 的唯一载荷。只说「越界了」等于白烧一轮。"""
    rep = review(("a.py", "b.py"), ("a.py",))
    c = rep.claims[0]
    assert c["check"] == "declared-paths-scope"
    assert "a.py" in c["expected"] and "b.py" in c["got"]
    assert c["command"]          # 人要能自己复现这个判断


def test_zero_cost_and_zero_tokens():
    """确定性监工。非零会污染 §5.1 的单位命中成本。"""
    rep = review(("a.py", "b.py"), ("a.py",))
    assert rep.cost_usd == 0.0 and rep.tokens == 0


def test_a_long_list_is_truncated_in_the_claim():
    """越界 200 个文件时把 200 条路径塞进 prompt，会挤掉真正的失败项。"""
    rep = review(tuple(f"f{i}.py" for i in range(30)), ("nope.py",))
    got = rep.claims[0]["got"]
    assert "另有 10 个" in got and "改了 30 个文件" in got


# ── 匹配语义 ──────────────────────────────────────────────────────────────

def test_a_directory_declaration_covers_files_under_it():
    """写目录名是最自然的写法，而 fnmatch 的 * 不跨 /。"""
    assert out_of_scope(("docs/readme.md", "docs/a/b.md"), ("docs/",)) == ()
    assert out_of_scope(("docs/readme.md",), ("docs",)) == ()


def test_a_glob_declaration_works_like_a_grading_rule():
    """和分级规则同一套 fnmatch 语义：同一个模式在两处必须匹配同一批文件。"""
    assert out_of_scope(("src/util/a.py",), ("src/util/*.py",)) == ()
    assert out_of_scope(("src/other/a.py",), ("src/util/*.py",)) == \
        ("src/other/a.py",)


def test_a_directory_declaration_does_not_match_a_prefix_sibling():
    """声明 docs/ 不该顺带放过 docs-internal/ —— 前缀相同但是另一个目录。"""
    assert out_of_scope(("docs-internal/x.md",), ("docs/",)) == \
        ("docs-internal/x.md",)


def test_out_of_scope_is_deduped_and_ordered():
    """claim 里同一个路径出现两次会让人以为改了两个文件。"""
    assert out_of_scope(("b.py", "a.py", "b.py"), ("x.py",)) == \
        ("b.py", "a.py")


def test_an_exact_file_declaration_does_not_cover_its_siblings():
    """声明 src/util/text.py 不等于放开 src/util/ ——
    这正是实测漏掉的那个洞（顺手改了 src/util/other.py）。"""
    assert out_of_scope(("src/util/other.py",), ("src/util/text.py",)) == \
        ("src/util/other.py",)


# ── 接进 dispatcher ──────────────────────────────────────────────────────
#
# 这一段测的是接线：范围监工要走**打回**路径，不是后分级的升级路径。
# 越界是 worker 自己能修的（把无关文件改回去），而后分级的升级不打回。

from factory.audit.store import AuditStore   # noqa: E402
from factory.dispatcher import Dispatcher, Outcome   # noqa: E402
from tests.test_dispatcher import (  # noqa: E402
    AlwaysPass, FakeAdapter, _result, _task,
)


def build(tmp_path, paths):
    store = AuditStore(tmp_path / "a.db")
    adapter = FakeAdapter([_result(paths=paths)] * 3)
    d = Dispatcher(adapter=adapter, store=store, supervisor=AlwaysPass())
    return d, store, adapter


def test_scope_creep_blocks_a_merge(tmp_path):
    """实测过的洞：声明改一个文件、顺手改了另外两个，三个都不匹配任何
    分级规则，后分级仍是 A 类，干净合并。分级引擎管危险不管离题。"""
    d, store, _ = build(tmp_path, ("greet.py", "docs/readme.md"))
    rep = d.run(_task(), tmp_path)
    assert rep.outcome == Outcome.ESCALATED
    assert "docs/readme.md" in rep.escalation_reason


def test_the_worker_is_told_which_files_to_revert(tmp_path):
    """打回而不是直接上人：这是 worker 修得了的问题。"""
    d, _store, adapter = build(tmp_path, ("greet.py", "docs/readme.md"))
    d.run(_task(), tmp_path)
    assert len(adapter.calls) == 3           # 用完三轮才上人
    assert "docs/readme.md" in adapter.calls[1][0]


def test_staying_in_scope_still_merges(tmp_path):
    d, _store, _ = build(tmp_path, ("greet.py",))
    assert d.run(_task(), tmp_path).outcome == Outcome.MERGED


def test_an_undeclared_task_is_not_blocked_by_scope(tmp_path):
    """既有任务绝大多数没写 declared_paths。默认开启这个监工不能给它们加新的红。"""
    d, _store, _ = build(tmp_path, ("greet.py", "whatever.py"))
    rep = d.run(_task(declared_paths=()), tmp_path)
    assert rep.outcome == Outcome.MERGED


def test_a_hard_gated_task_is_never_reached_by_the_scope_check(tmp_path):
    """D 类在派发前就拦住，压根没有 diff 可比。范围监工不能成为绕过硬闸门的路径。"""
    d, _store, adapter = build(tmp_path, ("greet.py",))
    rep = d.run(_task(declared_ops=("prod_deploy",)), tmp_path)
    assert rep.outcome == Outcome.BLOCKED_HARD_GATE
    assert adapter.calls == []
