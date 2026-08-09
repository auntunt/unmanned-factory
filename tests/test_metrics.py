"""spec §5.1：每个监工三个数 —— 命中率、漏报数、单位命中成本。"""
import pytest

from factory.audit.models import (
    NOT_DISPATCHED,
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.metrics import gate3_rework, supervisor_metrics


@pytest.fixture
def store():
    return AuditStore(":memory:")


def _attempt(store, task_id, *, verdicts, resolution, defects=()):
    """造一条 attempt。verdicts 是 [(role, verdict, cost), ...]。"""
    aid = store.open_attempt(
        task_id=task_id, spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    for role, verdict, cost in verdicts:
        store.record_verdict(aid, role=role, verdict=verdict, claims=[],
                             tokens=int(cost * 1000), cost_usd=cost)
    store.finalize(aid, resolution)
    for d in defects:
        store.link_defect(aid, d)
    return aid


def test_no_data_yields_empty_report(store):
    assert supervisor_metrics(store) == {}


def test_fail_then_reworked_counts_as_true_positive(store):
    _attempt(store, "T-1",
             verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.10)],
             resolution=Resolution.REWORKED)
    m = supervisor_metrics(store)["spec"]
    assert m.fired == 1
    assert m.true_positives == 1
    assert m.false_positives == 0
    assert m.hit_rate == 1.0


def test_human_override_marks_the_alarm_false(store):
    """人直接覆盖合并 → 那条 FAIL 是假阳性（spec §5）。"""
    _attempt(store, "T-2",
             verdicts=[(SupervisorRole.ARCHITECTURE, Verdict.FAIL, 0.20)],
             resolution=Resolution.HUMAN_OVERRIDE)
    m = supervisor_metrics(store)["architecture"]
    assert m.fired == 1
    assert m.true_positives == 0
    assert m.false_positives == 1
    assert m.hit_rate == 0.0


def test_escalated_is_unadjudicated_not_a_hit(store):
    """3 轮不过升级给人，人还没判 → 不能算真阳性也不能算假阳性。

    否则命中率会被未定案的样本污染，两周后的裁剪判据就建在假数字上。
    """
    _attempt(store, "T-3",
             verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.10)],
             resolution=Resolution.ESCALATED)
    m = supervisor_metrics(store)["spec"]
    assert m.fired == 1
    assert m.unadjudicated == 1
    assert m.true_positives == 0
    assert m.false_positives == 0
    assert m.hit_rate is None          # 分母为 0，不许编一个数出来


def test_pass_plus_linked_defect_is_a_false_negative(store):
    """监工放过、事后炸了 → 漏报。缺这个只能优化误报，永远看不见漏报。"""
    _attempt(store, "T-4",
             verdicts=[(SupervisorRole.REGRESSION, Verdict.PASS, 0.0)],
             resolution=Resolution.MERGED, defects=("BUG-1",))
    m = supervisor_metrics(store)["regression"]
    assert m.passed == 1
    assert m.false_negatives == 1


def test_pass_without_defect_is_not_a_false_negative(store):
    _attempt(store, "T-5",
             verdicts=[(SupervisorRole.REGRESSION, Verdict.PASS, 0.0)],
             resolution=Resolution.MERGED)
    assert supervisor_metrics(store)["regression"].false_negatives == 0


def test_merged_despite_fail_is_a_false_positive(store):
    """同一轮里另一个监工放行、这条 FAIL 被无视 → 也算假阳性。"""
    _attempt(store, "T-6",
             verdicts=[(SupervisorRole.ARCHITECTURE, Verdict.FAIL, 0.05)],
             resolution=Resolution.MERGED)
    assert supervisor_metrics(store)["architecture"].false_positives == 1


def test_cost_accumulates_across_rounds_and_roles(store):
    _attempt(store, "T-7",
             verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.10),
                       (SupervisorRole.ARCHITECTURE, Verdict.FAIL, 0.30)],
             resolution=Resolution.REWORKED)
    _attempt(store, "T-7",
             verdicts=[(SupervisorRole.SPEC, Verdict.PASS, 0.12),
                       (SupervisorRole.ARCHITECTURE, Verdict.PASS, 0.28)],
             resolution=Resolution.MERGED)
    m = supervisor_metrics(store)
    assert m["spec"].cost_usd == pytest.approx(0.22)
    assert m["architecture"].cost_usd == pytest.approx(0.58)
    assert m["spec"].tokens == 220
    # 单位命中成本 = 总成本 / 真阳性数
    assert m["spec"].cost_per_hit == pytest.approx(0.22)


def test_cost_per_hit_is_none_with_zero_hits(store):
    _attempt(store, "T-8",
             verdicts=[(SupervisorRole.SPEC, Verdict.PASS, 0.50)],
             resolution=Resolution.MERGED)
    assert supervisor_metrics(store)["spec"].cost_per_hit is None


def test_scoped_to_one_task(store):
    _attempt(store, "T-a", verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.1)],
             resolution=Resolution.REWORKED)
    _attempt(store, "T-b", verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.1)],
             resolution=Resolution.REWORKED)
    assert supervisor_metrics(store)["spec"].fired == 2
    assert supervisor_metrics(store, task_id="T-a")["spec"].fired == 1


@pytest.mark.parametrize("verdicts,resolution,defects,expected", [
    ([(SupervisorRole.SPEC, Verdict.FAIL, 0.01)], Resolution.HUMAN_OVERRIDE, (),
     "命中率低且无漏报"),
    # 从没报对过却漏了东西 = 没干活。不能因为分母为 0 就落到「保留」
    ([(SupervisorRole.SPEC, Verdict.PASS, 0.01)], Resolution.MERGED, ("B-1",),
     "它没干活"),
    ([(SupervisorRole.SPEC, Verdict.FAIL, 0.01)], Resolution.REWORKED, (),
     "保留"),
    ([(SupervisorRole.SPEC, Verdict.FAIL, 5.0)], Resolution.REWORKED, (),
     "成本高"),
])
def test_trim_verdict_follows_spec_5_1(store, verdicts, resolution, defects,
                                      expected):
    _attempt(store, "T-v", verdicts=verdicts, resolution=resolution,
             defects=defects)
    assert expected in supervisor_metrics(store)["spec"].verdict_line()


# ---------- P1 判据：闸门 3 上人平均打回次数 ≤ 1 ----------

def test_gate3_rework_is_zero_when_everything_merges_first_round(store):
    _attempt(store, "T-g1",
             verdicts=[(SupervisorRole.REGRESSION, Verdict.PASS, 0.0)],
             resolution=Resolution.MERGED)
    g = gate3_rework(store)
    assert g.tasks == 1
    assert g.total_reworks == 0
    assert g.mean_reworks == 0.0
    assert g.meets_p1_target is True


def test_gate3_counts_reworks_per_task_not_per_attempt(store):
    """两轮返工后合并 → 这个任务打回 2 次，不是 3 次 attempt。"""
    for _ in range(2):
        _attempt(store, "T-g2",
                 verdicts=[(SupervisorRole.SPEC, Verdict.FAIL, 0.1)],
                 resolution=Resolution.REWORKED)
    _attempt(store, "T-g2",
             verdicts=[(SupervisorRole.SPEC, Verdict.PASS, 0.1)],
             resolution=Resolution.MERGED)
    g = gate3_rework(store)
    assert g.tasks == 1
    assert g.total_reworks == 2
    assert g.mean_reworks == 2.0
    assert g.meets_p1_target is False       # 2 > 1


def test_gate3_averages_across_tasks(store):
    _attempt(store, "T-g3", verdicts=[], resolution=Resolution.MERGED)
    _attempt(store, "T-g4", verdicts=[], resolution=Resolution.REWORKED)
    _attempt(store, "T-g4", verdicts=[], resolution=Resolution.MERGED)
    g = gate3_rework(store)
    assert g.tasks == 2
    assert g.mean_reworks == 0.5
    assert g.meets_p1_target is True


def test_gate3_does_not_count_reworks_caused_by_upstream_errors(store):
    """网关 502 导致的打回不进闸门 3 的分子。

    这个指标检验的是**验收条件够不够机器可判定** —— 一次上游抖动对那件事一个字
    都没说。不排掉的话，上游越不稳这个数越差，人会拿着它去改验收条件，
    而验收条件根本没问题。同一个形状在 P0 判据上踩过一次。

    非空基线：同一个任务里再放一次**真**打回，证明分子本来就在数。缺了它，
    把 total_reworks 改成恒 0 也能让下面全绿。
    """
    aid = store.open_attempt(
        task_id="T-g502", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "harness", "expected": "exit_status ok",
                 "got": "API Error: 502"}])
    store.finalize(aid, Resolution.REWORKED)
    # 基线：一次真打回（判据是验收条件本身没被满足）
    _attempt(store, "T-g502",
             verdicts=[(SupervisorRole.REGRESSION, Verdict.FAIL, 0.1)],
             resolution=Resolution.REWORKED)
    _attempt(store, "T-g502", verdicts=[], resolution=Resolution.MERGED)

    g = gate3_rework(store)
    assert g.tasks == 1
    assert g.total_reworks == 1          # 只数那次真打回，不是 2
    assert g.mean_reworks == 1.0
    assert g.upstream_reworks == 1        # 但排掉了多少要看得见


def test_gate3_excludes_tasks_blocked_before_dispatch(store):
    """C/D 类预分级拦下的任务从没进过闸门 3，不能进分母。

    否则拦得越多、平均打回次数看着越好，指标会奖励错误的行为。
    """
    _attempt(store, "T-g5", verdicts=[], resolution=Resolution.REWORKED)
    _attempt(store, "T-g5", verdicts=[], resolution=Resolution.MERGED)
    aid = store.open_attempt(
        task_id="T-blocked", spec_ref=[], oracle_class=OracleClass.D,
        class_reason="D: prod_deploy", harness="h",
        harness_version=NOT_DISPATCHED,   # dispatcher 拦下时就是这个值
        model="opus",
    )
    store.finalize(aid, Resolution.ESCALATED)
    g = gate3_rework(store)
    assert g.tasks == 1, "被预分级拦下的任务不该进分母"
    assert g.mean_reworks == 1.0


def test_gate3_on_empty_db(store):
    g = gate3_rework(store)
    assert g.tasks == 0
    assert g.mean_reworks is None
    assert g.meets_p1_target is None     # 没数据不等于达标


def test_gate3_exclusion_holds_against_the_real_dispatcher(tmp_path):
    """不用手造行：真跑一次 D 类拦截，确认它不进闸门 3 分母。

    手造的 harness_version 可能和 dispatcher 实际写的值不一致，
    那样这条排除规则就是假通过的。
    """
    from factory.dispatcher import Dispatcher, Outcome
    from factory.task import CheckSpec, Task

    class NeverCalled:
        name = "claude_code"

        def run(self, *a, **kw):        # pragma: no cover
            raise AssertionError("D 类不该派发")

    s = AuditStore(tmp_path / "g.db")
    d = Dispatcher(adapter=NeverCalled(), store=s)
    task = Task(task_id="T-hardgate", prompt="deploy",
                declared_ops=("prod_deploy",),
                checks=(CheckSpec(name="noop", command="true"),))
    assert d.run(task, tmp_path).outcome == Outcome.BLOCKED_HARD_GATE

    g = gate3_rework(s)
    assert g.tasks == 0
    assert g.mean_reworks is None


# ---------- 监工故障不是告警 ----------

def _fault_claim(role="spec"):
    return [{"check": f"supervisor-{role}-unavailable", "expected": "裁决",
             "got": "timeout after 300s"}]


def test_supervisor_fault_does_not_count_as_an_alarm(store):
    """超时不是「它报了个警」。混进 fired 会污染命中率分母，
    而裁剪决定就建在那个分母上。"""
    aid = store.open_attempt(
        task_id="T-f", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    store.record_verdict(aid, role=SupervisorRole.SPEC, verdict=Verdict.FAIL,
                         claims=_fault_claim(), cost_usd=0.05)
    store.finalize(aid, Resolution.ESCALATED)

    m = supervisor_metrics(store)["spec"]
    assert m.faults == 1
    assert m.fired == 0
    assert m.unadjudicated == 0
    assert m.hit_rate is None
    assert m.cost_usd == 0.05        # 烧掉的钱还是要记


def test_faults_dominating_says_fix_availability_first(store):
    aid = store.open_attempt(
        task_id="T-f2", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    store.record_verdict(aid, role=SupervisorRole.SPEC, verdict=Verdict.FAIL,
                         claims=_fault_claim())
    store.finalize(aid, Resolution.ESCALATED)
    assert "先修可用性" in supervisor_metrics(store)["spec"].verdict_line()


def test_upstream_502_is_not_a_true_positive_for_the_supervisor(store):
    """网关 502 不许给回归监工记真阳性。

    真跑批抓到的：上游回 502，dispatcher 的 `_blocked("harness", ...)` 挂在
    REGRESSION 名下，attempt 落成 reworked，于是回归监工白得一次真阳性、命中率
    100%。它什么都没审出来 —— 那次红是我们这一侧的网络。

    而这个数正是用来决定「这个监工值不值它的钱」的，虚高的方向恰好是「保留」，
    也就是不会有人来纠的那一侧。

    非空基线：走的是和真告警**完全一样**的写入路径（同一个 role、同一个
    Verdict.FAIL、同一个 REWORKED），差别只在 check 名。所以这条测试排除掉了
    「它本来就不会被算进去」这种解释。
    """
    aid = store.open_attempt(
        task_id="T-502", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "harness", "command": "claude_code",
                 "expected": "exit_status ok",
                 "got": "error: API Error: 502 Upstream request failed"}],
    )
    store.finalize(aid, Resolution.REWORKED)

    m = supervisor_metrics(store)["regression"]
    assert m.true_positives == 0
    assert m.fired == 0
    assert m.hit_rate is None          # 不是 1.0
    assert m.harness_faults == 1       # 但没被丢掉
    assert m.faults == 0               # 也不算「监工自己坏了」


def test_upstream_fault_verdict_points_at_the_gateway_not_the_supervisor(store):
    """建议要说「看网关」，不能说「先修监工可用性」。

    说错方向会把人送去翻监工日志，而那里什么都没有 —— 一条指错方向的建议比
    没有建议更费时间。这两种故障对人是同一个动作（不打回 worker），
    但指向的修法完全相反，所以计数器也必须是两个。
    """
    aid = store.open_attempt(
        task_id="T-502b", spec_ref=[], oracle_class=OracleClass.A,
        class_reason="A", harness="h", harness_version="v", model="haiku",
    )
    store.record_verdict(
        aid, role=SupervisorRole.REGRESSION, verdict=Verdict.FAIL,
        claims=[{"check": "harness", "expected": "exit_status ok",
                 "got": "API Error: 502"}],
    )
    store.finalize(aid, Resolution.ESCALATED)

    line = supervisor_metrics(store)["regression"].verdict_line()
    assert "网关" in line
    assert "先修可用性" not in line


def test_a_real_alarm_alongside_a_fault_still_counts(store):
    """故障归故障，真报的警照算 —— 两者分开计数而不是互相吞掉。"""
    for claims, res in ((_fault_claim(), Resolution.ESCALATED),
                        ([{"check": "AC-1", "expected": "x", "got": "y"}],
                         Resolution.REWORKED)):
        aid = store.open_attempt(
            task_id="T-f3", spec_ref=[], oracle_class=OracleClass.A,
            class_reason="A", harness="h", harness_version="v", model="haiku",
        )
        store.record_verdict(aid, role=SupervisorRole.SPEC,
                             verdict=Verdict.FAIL, claims=claims)
        store.finalize(aid, res)

    m = supervisor_metrics(store)["spec"]
    assert (m.faults, m.fired, m.true_positives) == (1, 1, 1)
    assert m.hit_rate == 1.0
