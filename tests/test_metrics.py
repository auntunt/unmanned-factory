"""spec §5.1：每个监工三个数 —— 命中率、漏报数、单位命中成本。"""
import pytest

from factory.audit.models import (
    OracleClass,
    Resolution,
    SupervisorRole,
    Verdict,
)
from factory.audit.store import AuditStore
from factory.metrics import supervisor_metrics


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
