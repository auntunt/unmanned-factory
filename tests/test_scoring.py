"""综合评分的测试。

重点不在算术对不对（那是三个乘法），而在**缺数据时不许编分**：
这个模块唯一有价值的行为是拒绝给出看起来像结论的假总分。
"""
from __future__ import annotations

import pytest

from factory.scoring import (
    COST_BASELINE_USD,
    W_COST,
    W_HUMAN,
    W_PRECISION,
    ScoreInput,
    compute_score,
)


def test_权重和为一():
    """三个默认权重必须和为 1，否则总分刻度会悄悄变。"""
    assert abs(W_HUMAN + W_PRECISION + W_COST - 1.0) < 1e-9


def test_满分():
    s = compute_score(ScoreInput(
        human_passed=10, human_failed=0,
        true_positives=10, false_positives=0,
        avg_cost_per_task_usd=0.0,
    ))
    assert s.total == pytest.approx(100.0)
    assert s.grade == "A"


def test_零分():
    """全不通过 + 全误报 + 花费超参考线 = 0 分，不是负分。"""
    s = compute_score(ScoreInput(
        human_passed=0, human_failed=5,
        true_positives=0, false_positives=5,
        avg_cost_per_task_usd=COST_BASELINE_USD * 3,
    ))
    assert s.total == pytest.approx(0.0)
    assert s.grade == "D"


def test_成本超参考线不给负分():
    """花 10 倍参考线也只扣到 0 —— 负分会把另外两项的得分吃掉，
    让一个验收全通过的配置显示成负数。"""
    s = compute_score(ScoreInput(
        human_passed=10, human_failed=0,
        true_positives=10, false_positives=0,
        avg_cost_per_task_usd=COST_BASELINE_USD * 10,
    ))
    assert s.sub["成本效率"] == 0.0
    # 人工验收 + 监工精度 满分，成本 0 → 80 分
    assert s.total == pytest.approx(80.0)


@pytest.mark.parametrize("kwargs,缺项", [
    # 没人验收过 → 缺人工验收
    (dict(true_positives=5, false_positives=5, avg_cost_per_task_usd=0.5),
     "人工验收"),
    # 监工一次都没定案 → 缺监工已定案
    (dict(human_passed=5, human_failed=1, avg_cost_per_task_usd=0.5),
     "监工已定案"),
    # 一个任务都没跑过 → 缺成本数据
    (dict(human_passed=5, human_failed=1, true_positives=5, false_positives=5),
     "成本数据"),
])
def test_缺任一项都不给总分(kwargs, 缺项):
    """核心约束：缺数据时 total 必须是 None。

    编一个「暂按 0 计」的总分比不给分更糟 —— 它看起来像结论，
    实际是缺数据，而看板上不会写这个区别。
    """
    s = compute_score(ScoreInput(**kwargs))
    assert s.total is None
    assert 缺项 in s.missing
    assert s.grade == "n/a"
    # 有数据的那几项仍然给出来 —— 缺一项不该把已有信息也藏掉
    assert len(s.sub) == 2
    assert 缺项 in s.explain()


def test_全缺时列出三项():
    s = compute_score(ScoreInput())
    assert s.total is None
    assert len(s.missing) == 3
    assert s.sub == {}


def test_未验收的轮次不进分母():
    """8 通过 2 不通过 = 80%，即使还有 90 轮没验收。

    没人验过的轮次算成不通过会让通过率虚低，而虚低的方向正好是
    「这套系统不行」—— 那是会被当真的那一侧。
    """
    s = compute_score(ScoreInput(
        human_passed=8, human_failed=2,
        true_positives=1, false_positives=0,
        avg_cost_per_task_usd=0.0,
    ))
    assert s.sub["人工验收"] == pytest.approx(0.8)


def test_权重不合一报错():
    """外部传的权重也必须和为 1，否则两次调用的「85 分」不是一回事。"""
    with pytest.raises(ValueError, match="权重必须和为 1"):
        compute_score(
            ScoreInput(human_passed=1, true_positives=1,
                       avg_cost_per_task_usd=0.0),
            w_human=0.5, w_precision=0.5, w_cost=0.5,
        )


def test_等级分界():
    """85/70/55 是 A/B/C 的下界，边界值取上一档。"""
    def 分(total: float) -> str:
        # 直接构造：走 compute_score 凑不出精确的边界值
        from factory.scoring import Score
        return Score(total=total).grade

    assert 分(85.0) == "A"
    assert 分(84.9) == "B"
    assert 分(70.0) == "B"
    assert 分(69.9) == "C"
    assert 分(55.0) == "C"
    assert 分(54.9) == "D"
