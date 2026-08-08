"""guard 测试。这些是入口层的安全测试，不是功能测试。

核心不变量只有一条：**guard 只增不减**。模型说"没有不可逆操作"不能
把 guard 扫出的 prod_deploy 抹掉 —— 否则 D 类硬闸门就有了一条
"让模型漏判" 的旁路。
"""

from __future__ import annotations

import pytest
import yaml

from factory.grading.rules import GradingEngine
from factory.intake.guard import KNOWN_OPS, harden_ops, scan_ops


def ops(text: str) -> set[str]:
    return {f.op for f in scan_ops(text)}


# ── 中文：真实用户会怎么说 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("改完之后上线", "prod_deploy"),
    ("发布到线上环境", "prod_deploy"),
    ("部署到生产", "prod_deploy"),
    ("先灰度发布看看", "prod_deploy"),
    ("给 users 表加个字段", "schema_migration"),
    ("要做数据库迁移", "schema_migration"),
    ("把旧的订单数据删除", "data_delete"),
    ("清空一下表", "truncate"),
    ("直接强推到 main", "force_push"),
    ("改写 git 历史", "force_push"),
    ("推送镜像到仓库", "registry_push"),
    ("新增一个设置页面", "new_ux"),
    ("改一下配色", "visual_change"),
])
def test_chinese_phrasings_are_caught(text, expected):
    assert expected in ops(text), f"{text!r} 没扫出 {expected}"


@pytest.mark.parametrize("text,expected", [
    ("then deploy to production", "prod_deploy"),
    ("ship to prod when green", "prod_deploy"),
    ("run the migration first", "schema_migration"),
    ("add column email to users", "schema_migration"),
    ("delete all records older than 30d", "data_delete"),
    ("truncate the staging table", "truncate"),
    ("force-push the rebased branch", "force_push"),
    ("docker push the built image", "registry_push"),
    ("add a new modal for confirmation", "new_ux"),
    ("change the layout a bit", "visual_change"),
])
def test_english_phrasings_are_caught(text, expected):
    assert expected in ops(text), f"{text!r} 没扫出 {expected}"


def test_plain_refactor_triggers_nothing():
    """A 类任务不能被误判成 C/D，否则人人都要上人，无人工厂就白做了。"""
    assert ops("把 slugify 函数抽到 text.py，加两个单测") == set()
    assert ops("修一个 off-by-one，range 少了一位") == set()


# ── 只增不减 ─────────────────────────────────────────────────────────────

def test_guard_adds_ops_the_model_missed():
    """模型报空数组，guard 照样把 prod_deploy 加上。这是本文件的主命题。"""
    text = "把重试逻辑改成指数退避，改完直接上线"
    result, findings = harden_ops(text, [])
    assert "prod_deploy" in result
    assert [f.op for f in findings] == ["prod_deploy"]


def test_guard_keeps_model_declared_ops_it_cannot_see():
    """模型能读懂绕弯的说法，guard 读不懂。所以取并集，不是二选一。"""
    text = "把那批老东西处理掉"          # guard 扫不出
    result, findings = harden_ops(text, ["data_delete"])
    assert result == ("data_delete",)
    assert findings == (), "模型自己报的不该算 guard 的功劳"


def test_union_of_both_sources():
    text = "删掉历史订单，然后上线"
    result, _ = harden_ops(text, ["schema_migration"])
    assert set(result) >= {"schema_migration", "data_delete", "prod_deploy"}


def test_ops_are_deduped_and_model_order_preserved():
    text = "上线"
    result, findings = harden_ops(text, ["prod_deploy", "prod_deploy"])
    assert result == ("prod_deploy",)
    assert findings == (), "模型已报的不重复加"


def test_blank_declared_entries_are_dropped():
    result, _ = harden_ops("重构一下", ["", "  ", "new_ux"])
    assert result == ("new_ux",)


# ── 证据 ─────────────────────────────────────────────────────────────────

def test_finding_carries_the_triggering_phrase():
    """没证据的自动升级会让人想关掉 guard。trigger 必须能定位到原话。"""
    (finding,) = scan_ops("先把 retry 改好，明天上线到生产环境")
    assert finding.op == "prod_deploy"
    assert "上线" in finding.trigger
    assert finding.pattern


def test_one_finding_per_op_even_with_many_matches():
    text = "上线，上线，再上线，然后 deploy to production"
    assert len([f for f in scan_ops(text) if f.op == "prod_deploy"]) == 1


def test_findings_sorted_for_stable_output():
    text = "上线并且清空表并且强推"
    got = [f.op for f in scan_ops(text)]
    assert got == sorted(got), "顺序不稳定，YAML 注释块每次跑会变"


# ── 故意的取舍：不做否定判断 ──────────────────────────────────────────────

def test_negation_still_fires_and_that_is_deliberate():
    """「不要上线」也会命中。

    正确处理否定要做语义分析，而不敢依赖模型语义判断正是 guard 存在的理由。
    多报由人一眼否掉（YAML 注释里写了怎么删）；漏报是无人管道自己动生产库。
    这条测试是那个取舍的锚点 —— 想"修"它之前先想清楚谁来兜漏报。
    """
    assert "prod_deploy" in ops("这次不要上线，只改代码")


# ── 和分级规则库对齐 ─────────────────────────────────────────────────────

def test_guard_ops_all_exist_in_the_grading_rules():
    """扫出来的 op 名字必须能被 oracle_rules.yaml 匹配上。

    名字写错的话 guard 照样报、审计照样记，但分级引擎一个都匹配不上 ——
    看起来全都在工作，实际闸门是空的。这是最难发现的一种失效。
    """
    doc = yaml.safe_load(
        open("factory/grading/oracle_rules.yaml", encoding="utf-8").read()
    )
    known = {o for r in doc["rules"] for o in r.get("ops", ())}
    missing = set(KNOWN_OPS) - known
    assert not missing, f"guard 会报但规则库不认的 op：{missing}"


@pytest.mark.parametrize("text,expected_class", [
    ("改完上线", "D"),
    ("给表加字段", "D"),
    ("删掉所有历史数据", "D"),
    ("新增一个设置页面", "C"),
    ("改一下配色", "C"),
])
def test_end_to_end_guard_to_grade(text, expected_class):
    """真正要保证的是这个：说了那句话 → 任务真的被判成 C/D。"""
    result, _ = harden_ops(text, [])
    grade = GradingEngine.default().grade(paths=(), ops=result)
    assert grade.oracle_class.value == expected_class, grade.reason


def test_d_class_from_guard_is_a_hard_gate():
    result, _ = harden_ops("跑完测试就上线到生产", [])
    grade = GradingEngine.default().grade(paths=(), ops=result)
    assert grade.hard_gate is True
    assert grade.unmanned_allowed is False


def test_c_class_from_guard_is_never_unmanned():
    result, _ = harden_ops("加一个新的确认弹窗", [])
    grade = GradingEngine.default().grade(paths=(), ops=result)
    assert grade.unmanned_allowed is False
    assert grade.hard_gate is False, "C 类要上人但不是硬闸门"


# --- guard 在 dispatcher 里也要跑一遍（不只在 prd 抽取时） -------------------
#
# 上面那些测的是 guard 这个函数对不对。下面测的是**它有没有接在派发路上**。
# 这两件事在报表上长得一样：guard 单测全绿、D 类闸门单测全绿，而
# `factory queue 一份人手写的 YAML` 从头到尾没碰过 guard。

def _handwritten(tmp_path, prompt, ops=()):
    """人手写的 task.yaml —— 这条路不过 TaskExtractor，所以不过 harden_ops。"""
    from factory.task import CheckSpec, Task
    return Task(
        task_id="T-hand", prompt=prompt,
        declared_paths=("db/migrate.py",),
        declared_ops=tuple(ops),
        checks=(CheckSpec(name="ok", command="true"),),
    )


def test_a_handwritten_yaml_that_says_force_push_is_still_hard_gated(tmp_path):
    """declared_ops 空着、prompt 里写着 force push —— 必须不派发。

    修之前：dispatcher 直接拿 YAML 的 declared_ops 去分级，空的 →
    「no rule matched -> default A」→ 无人放行，adapter 真被调起来。
    走 `factory prd` 的同一份任务是 D 类硬闸门。两条入口相反判决。
    """
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher, Outcome
    from tests.test_dispatcher import AlwaysPass, FakeAdapter, _result

    adapter = FakeAdapter([_result()])
    d = Dispatcher(adapter=adapter,
                   store=AuditStore(tmp_path / "a.db"),
                   supervisor=AlwaysPass())
    rep = d.run(_handwritten(
        tmp_path, "把生产库的 users 表 drop 掉再重建，然后 force push 到 main"),
        tmp_path)

    assert rep.outcome == Outcome.BLOCKED_HARD_GATE
    assert rep.final_grade.oracle_class.value == "D"
    assert "force_push" in rep.escalation_reason
    assert not adapter.calls, "D 类任何情况下不许调 harness"


def test_the_two_entry_points_grade_the_same_task_the_same_way(tmp_path):
    """prd 路径和 queue 路径对同一份任务的分级必须一致。

    这条是上面那个 bug 的形状本身：不变量不是「D 类会被拦」，
    而是「同一份任务走哪条门进来，分级都一样」。
    """
    from factory.grading.rules import GradingEngine
    from factory.dispatcher import Dispatcher
    from factory.audit.store import AuditStore
    from tests.test_dispatcher import AlwaysPass, FakeAdapter, _result

    p = "上线前记得 force push 一下"
    via_prd, _ = harden_ops(p, ())          # 抽取时补过 ops
    grade_prd = GradingEngine.default().grade(("db/migrate.py",), via_prd)

    d = Dispatcher(adapter=FakeAdapter([_result()]),
                   store=AuditStore(tmp_path / "b.db"),
                   supervisor=AlwaysPass())
    grade_queue = d.run(_handwritten(tmp_path, p), tmp_path).final_grade

    assert grade_queue.oracle_class == grade_prd.oracle_class
    assert grade_queue.unmanned_allowed == grade_prd.unmanned_allowed


def test_the_post_grade_reads_the_same_ops_as_the_pre_grade(tmp_path):
    """后分级必须走 _ops，不是 task.declared_ops。

    这条**只能用替身测**，因为当前词表下它没有可观测行为：KNOWN_OPS 里
    每一个 op 都是 C 或 D（实测），全都 unmanned_allowed=False，所以
    guard 扫出非空 ops 的任务在预分级就被拦了，后分级根本走不到。
    第一版测试因此是假的 —— 它用 `rm -rf`（**不在词表里**）当输入，两次
    分级都拿到空元组，把「后分级退回读 YAML」的变异放了过去。

    那为什么还要改后分级：这行的价值是**在词表长出一个 A/B 类 op 的那天
    不会静默失守**。往 KNOWN_OPS 加一个允许无人的 op 是完全合理的演进
    （比如「改了 CHANGELOG」这种只想记账的标记），那天后分级会第一次真的
    收到非空 ops，而如果它还在读 YAML，就又漏了。替身盯的是接线本身。
    """
    from factory.audit.store import AuditStore
    from factory.dispatcher import Dispatcher
    from tests.test_dispatcher import AlwaysPass, FakeAdapter, _result

    seen: list[tuple[str, ...]] = []
    d = Dispatcher(adapter=FakeAdapter([_result()]),
                   store=AuditStore(tmp_path / "c.db"),
                   supervisor=AlwaysPass())
    real = d._engine.grade
    d._engine.grade = lambda paths, ops: (seen.append(tuple(ops))
                                          or real(paths, ops))

    # 假设词表明天多了一个 A 类 op：guard 扫得出、预分级放行。
    # 用替身模拟这一天，因为今天的 KNOWN_OPS 里没有这样的 op。
    import factory.dispatcher as dm
    monkey = lambda text, declared=(): (("future_marker_op",), ())
    orig = dm.harden_ops
    dm.harden_ops = monkey
    try:
        d.run(_handwritten(tmp_path, "随便改点东西"), tmp_path)
    finally:
        dm.harden_ops = orig

    assert len(seen) >= 2, f"该有预分级和后分级两次，实际 {len(seen)} 次"
    assert all(o == ("future_marker_op",) for o in seen), (
        f"有一次分级没走 _ops，拿到的是 YAML 里的原值: {seen}")
