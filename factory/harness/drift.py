"""`claude -p --output-format json` 的输出格式漂移检测。spec §10 风险 6。

放在 harness 层而不是 adapter 里：**读这个 JSON 的有两处**，
worker（`claude_code.py`）和模型监工（`supervisors/model_base.py`），
它们读的是同一个 CLI 的同一种输出。抄两份的话，只加固了便宜的那一路
—— 而贵的那一路是监工（P1 真跑单任务 $0.65 全落在监工上）。

为什么漂移这件事必须专门建一道闸：解析全是 `payload.get(k, default)`，
而 default 必然是「无事发生」那一侧。所以字段改名的每个降级方向都朝着
**看起来更好**：花费变 0、失败变成功、token 变 0。推论是漂移不可能靠
看报表发现 —— 报表越漂亮越可疑，而没人会去查一份漂亮的报表。
"""

from __future__ import annotations

#: 格式漂移的标记串。**承重**：漏账熔断器靠它认「这次花费记不上账」
#: （`factory/cli.py` 的 `is_untracked_spend`，那边 import 这个常量，
#: 不抄字面量）。
DRIFT_MARKER = "harness-format-drift"

#: 缺了就会**静默降级**的字段。不是「JSON 里所有字段」——
#: 只列那些缺失后果是「数字变 0 / 判断反转」而不是报错的。
#:
#: transcript / tool_calls / session_id 不在这里：它们缺失的后果是少一份
#: 证据或少一条近路，会在裁决或日志里看得见，不会伪装成一个正常的便宜任务。
LOAD_BEARING: tuple[tuple[str, str], ...] = (
    ("is_error", "成败判断会反转成永远成功"),
    ("total_cost_usd", "花费会记 0，预算上限形同虚设"),
    ("usage", "token 数全 0，单位成本指标失效"),
)


def missing_fields(payload: object) -> tuple[str, ...]:
    """列出承重字段里缺掉的那些。

    只判**键在不在**，不判值合不合理：`total_cost_usd: 0` 是完全正常的
    （确定性监工那一路、缓存命中、极短任务），把 0 当漂移会让熔断器
    天天误报，而一个天天误报的熔断器等于被关掉的熔断器。

    payload 不是 dict 时报全部缺失：那种情况下我们对这份输出一无所知，
    「一份读不懂的输出」和「缺了全部承重字段」应当走同一条路。
    """
    if not isinstance(payload, dict):
        return tuple(name for name, _ in LOAD_BEARING)
    return tuple(name for name, _ in LOAD_BEARING if name not in payload)


def drift_text(missing: tuple[str, ...]) -> str:
    """漂移的说明文本。带 DRIFT_MARKER，所以下游熔断器认得出来。

    统一在这里拼而不是各自拼：两处的文本必须都含 marker，
    而「记得带上 marker」这件事不该指望第三个调用点也记得。
    """
    return (f"{DRIFT_MARKER}: 输出 JSON 缺少承重字段 "
            f"{', '.join(missing)} —— harness 输出格式可能变了，"
            f"本次花费记不上账")


def total_tokens(payload: object) -> int:
    """这次调用真正烧掉的 input+output token。

    **优先 `modelUsage`，不是 `usage`。** 这不是风格选择，是实测出来的：
    同一次调用 `usage.input_tokens=27415`，而 `modelUsage` 里累计 77856。
    `usage` 只反映最后一轮，多轮调用（监工那种带 --json-schema 重试的）
    会系统性低估。判据是 `modelUsage` 里各模型 `costUSD` 之和与顶层
    `total_cost_usd` **完全相等**（实测 0.40520999999999996 两边一致），
    而 `usage` 和它对不上 —— 所以 modelUsage 才是账单那一侧的数。

    更糟的一种：监工的四个独立性 flag 全带上时（`--tools "" --safe-mode
    --exclude-dynamic-system-prompt-sections --json-schema`），
    `usage.input_tokens` 直接是 **0**，而 modelUsage 里是 2719。
    键在、值为 0 —— `missing_fields` 按设计看不见它（判 0 会天天误报），
    所以这个洞只能在取数这一侧堵。

    两边都取不到就返回 0：token 数不进预算闸门（花费走 total_cost_usd），
    它只喂单位成本指标。指标缺一格好过为它加一条会误报的熔断路径。
    """
    return sum(token_split(payload))


def token_split(payload: object) -> tuple[int, int]:
    """(input, output) 分开的版本。worker 那侧分两列落库。

    和 `total_tokens` 同一套优先级 —— 不是重复实现，而是同一个决定的两个
    出口。两处各自读 `usage` 的话，一处修好了另一处照旧低估，而低估的方向
    在报表上是「更便宜」，没人会来查。
    """
    if not isinstance(payload, dict):
        return (0, 0)
    mu = payload.get("modelUsage")
    if isinstance(mu, dict):
        i = o = 0
        for v in mu.values():
            if isinstance(v, dict):
                i += int(v.get("inputTokens") or 0)
                o += int(v.get("outputTokens") or 0)
        if i or o:
            return (i, o)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return (int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0))
    return (0, 0)
