"""综合评分：把「监工准不准」和「活干得好不好」合成一个数。

为什么要合成
------------
现在看板上有两组互不相干的数：监工命中率（metrics.py）说的是判据质量，
人工验收通过率说的是产出质量。演示时被问「所以这套东西到底行不行」，
指着两张表说「命中率 60%、验收通过 80%」不构成回答 —— 听的人没有权重，
没法把两个数合起来。

合成的代价是信息损失，所以这里的规矩是：**任何一项没数据就不给总分**，
返回 None 并说明缺哪一项。编一个「暂按 0 计」的总分比不给分更糟 ——
它看起来像结论，实际是缺数据。

权重
----
默认权重把人工验收放在监工命中率前面（0.5 : 0.3），理由是这套系统的产出
是代码而不是告警：监工全绿但人不认，说明判据写窄了，那是监工的问题；
监工全红但人都验收通过，说明判据写宽了，也是监工的问题。两种情形下
「人认不认」都是更靠近事实的那一端。

第三项 0.2 给成本效率，因为前两项都能靠「多跑几轮」刷上去 —— 没有成本项
的话，一个每个任务烧 5 美元最终通过的配置会打赢一个一次就过的配置。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 权重。改这里就改了总分的含义，所以三个数必须加起来是 1 —— 不然
# 「85 分」和上一版的「85 分」不是一回事，而没人会注意到刻度变了。
W_HUMAN = 0.5      # 人工验收通过率
W_PRECISION = 0.3  # 监工命中率
W_COST = 0.2       # 成本效率

#: 成本效率的参考线：一个任务花到这个数就算 0 分，0 花费算满分，中间线性。
#: 取 2.0 是因为实测单任务落在 0.2~0.8，参考线定在实测上界的 2~3 倍才
#: 不会让正常波动看起来像失控。
COST_BASELINE_USD = 2.0


@dataclass(frozen=True)
class ScoreInput:
    """算总分需要的原始数字。全部可缺，缺了就不给总分。"""

    #: 人工验收通过 / 已验收总数
    human_passed: int = 0
    human_failed: int = 0
    #: 监工已定案的真阳性 / 假阳性
    true_positives: int = 0
    false_positives: int = 0
    #: 平均每任务花费
    avg_cost_per_task_usd: float | None = None


@dataclass(frozen=True)
class Score:
    """一份评分。sub 里是各项得分（0~1），total 是加权后的 0~100。"""

    #: 加权总分 0~100。任何一项缺数据时为 None。
    total: float | None
    #: 各项得分，缺数据的项不出现在这个 dict 里
    sub: dict[str, float] = field(default_factory=dict)
    #: 缺了哪些项。total 为 None 时这里说明原因。
    missing: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        """给个字母等级，演示时比小数好念。没总分就说没总分。"""
        if self.total is None:
            return "n/a"
        if self.total >= 85:
            return "A"
        if self.total >= 70:
            return "B"
        if self.total >= 55:
            return "C"
        return "D"

    def explain(self) -> str:
        """一句话说明这个分怎么来的 / 为什么没有。"""
        if self.total is None:
            return "缺" + "、".join(self.missing) + " —— 补齐后才有总分"
        parts = [f"{k} {v * 100:.0f}" for k, v in self.sub.items()]
        return " + ".join(parts) + f" → {self.total:.0f}"


def compute_score(
    inp: ScoreInput,
    *,
    w_human: float = W_HUMAN,
    w_precision: float = W_PRECISION,
    w_cost: float = W_COST,
    cost_baseline: float = COST_BASELINE_USD,
) -> Score:
    """从原始数字算出加权总分。

    任何一项缺数据就不给总分 —— 编一个「暂按 0 计」的总分比不给分更糟，
    它看起来像结论，实际是缺数据。

    权重可以从外面传，但必须加起来是 1，否则报错 —— 不然「85 分」的
    含义在两次调用之间变了，而看板上不会写「本次权重 0.5/0.3/0.2」。
    """
    if abs(w_human + w_precision + w_cost - 1.0) > 1e-6:
        raise ValueError(
            f"权重必须和为 1，现在是 {w_human + w_precision + w_cost:.4f}"
        )

    sub = {}
    missing = []

    # 人工验收通过率
    judged = inp.human_passed + inp.human_failed
    if judged == 0:
        missing.append("人工验收")
    else:
        sub["人工验收"] = inp.human_passed / judged

    # 监工命中率
    adjudicated = inp.true_positives + inp.false_positives
    if adjudicated == 0:
        missing.append("监工已定案")
    else:
        sub["监工精度"] = inp.true_positives / adjudicated

    # 成本效率：avg=0 得满分，avg >= baseline 得 0 分，中间线性
    if inp.avg_cost_per_task_usd is None:
        missing.append("成本数据")
    else:
        # clamp 到 [0, 1]：超出 baseline 也只扣到 0，不给负分
        eff = max(0.0, min(1.0, 1 - inp.avg_cost_per_task_usd / cost_baseline))
        sub["成本效率"] = eff

    # 任何一项缺就不给总分
    if missing:
        return Score(total=None, sub=sub, missing=missing)

    total = (
        sub["人工验收"] * w_human
        + sub["监工精度"] * w_precision
        + sub["成本效率"] * w_cost
    ) * 100
    return Score(total=total, sub=sub, missing=[])
