"""确定性关键词扫描，补 declared_ops。**只增不减。**

为什么不让模型自己报 declared_ops 就完事：
分级引擎只看声明。声明里没有 prod_deploy，D 类闸门就不存在 —— 不是
"晚一点发现"，是永远不会发现。让一次模型调用成为硬闸门的唯一守门人，
等于给闸门开了一条后门（模型漏判 = 旁路）。Global Constraint 里
"D 类是硬闸门、非旁路" 管的就是这种间接旁路。

所以这里用最笨的办法：正则关键词表，中英双语，宁可多报。
多报的代价是人来看一眼（C/D 类本来就要上人）；少报的代价是
无人管道自己去动生产库。这个不对称决定了所有取舍。

ops 名字必须和 grading/oracle_rules.yaml 里的 ops 完全一致，否则
扫出来了也匹配不上规则。测试 test_guard_ops_all_exist_in_rules 钉住这一点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── 不可逆操作（D 类）────────────────────────────────────────────────────
# 每个 op 一组模式。中英分开写而不是拼一个大正则：某条报错时能看出是哪句话触发的。
_D_PATTERNS: dict[str, tuple[str, ...]] = {
    "prod_deploy": (
        r"上线", r"发布到?线上", r"部署到?(?:生产|正式|线上|prod)",
        r"推(?:到|上)生产", r"上生产", r"灰度发布", r"滚动(?:发布|更新)",
        r"\bdeploy(?:ing|ment)?\s+to\s+(?:prod|production|live)\b",
        r"\bship\s+to\s+prod", r"\bgo\s+live\b", r"\bproduction\s+release\b",
    ),
    "schema_migration": (
        r"(?:数据库|表结构|schema)\s*(?:迁移|变更|升级)",
        r"改表结构", r"加(?:一)?(?:个)?字段", r"删(?:一)?(?:个)?字段",
        r"\bmigrat(?:e|ion|ing)\b", r"\balter\s+table\b",
        r"\badd\s+column\b", r"\bdrop\s+column\b",
    ),
    "drop_table": (
        r"删(?:除)?(?:掉)?(?:整张)?表", r"表\s*删(?:除|掉)", r"drop\s+table", r"删库",
    ),
    "truncate": (
        r"清空(?:一下)?(?:整张)?(?:表|数据|库)", r"\btruncate\b",
    ),
    # 中文有「把 X 删除」这种宾语前置，动词在后。两个方向都要写：
    # 只写「删除+数据」会漏掉「把订单数据删除」，而这正是用户最自然的说法。
    # 修饰词会叠：「删掉所有历史数据」是 删掉 + 所有 + 历史 + 数据。
    # 写成 (?:...)? 只能吃一个，必须用 * 允许叠加。
    "data_delete": (
        r"删(?:除)?(?:掉)?(?:(?:所有|全部|历史|线上|旧|老|的)\s*)*"
        r"(?:数据|记录|用户|订单)",
        r"(?:数据|记录|用户|订单)\s*(?:都)?(?:给)?删(?:除|掉|了)",
        r"批量删除", r"清理(?:掉)?(?:线上|生产)?数据",
        r"\bdelete\s+(?:all|the)?\s*(?:rows|records|data|users)\b",
        r"\bpurge\b", r"\bwipe\b",
    ),
    "force_push": (
        r"强推", r"强制推送", r"force[-\s]?push", r"\bpush\s+-f\b",
        r"\breset\s+--hard\b", r"改写\s*(?:git\s*)?历史", r"重写\s*(?:git\s*)?历史",
    ),
    "registry_push": (
        r"推(?:送)?(?:镜像|image)", r"(?:镜像|image)\s*推(?:送)?",
        r"\bdocker\s+push\b", r"\bpush\s+(?:the\s+)?image\b",
        r"发(?:布)?(?:到)?(?:镜像)?仓库",
    ),
}

# ── 永不无人（C 类）──────────────────────────────────────────────────────
# 这两个 op 在 oracle_rules.yaml 里是 C 类：有廉价裁判判不了的东西
# （界面好不好看、交互对不对），所以判 C 而不是 D —— 要上人但不是硬闸门。
_C_PATTERNS: dict[str, tuple[str, ...]] = {
    # 「新增一个设置页面」中间隔着一个名词修饰语。中文里这一段是开放的
    # （设置 / 登录 / 订单详情…），所以用「非标点的少量字符」跨过去，
    # 而不是去枚举。[^，。；、\n]{0,6} 把跨句误命中挡在外面。
    "new_ux": (
        r"新(?:增|加|建)?(?:一)?(?:个)?[^，。；、\n]{0,6}?"
        r"(?:页面|界面|弹窗|对话框|表单|入口|按钮)",
        r"重做(?:界面|页面|交互)", r"新的?交互",
        r"\bnew\s+(?:page|screen|modal|dialog|form|flow)\b",
        r"\bredesign\b", r"\bUX\b",
    ),
    "visual_change": (
        r"改(?:一下)?(?:样式|配色|布局|字体|间距)", r"视觉(?:上的)?(?:调整|改动)",
        r"\brestyl(?:e|ing)\b", r"\bre-?theme\b",
        r"\bchange\s+the\s+(?:layout|colors?|styling|font)\b",
    ),
}

_ALL_PATTERNS = {**_D_PATTERNS, **_C_PATTERNS}

# 预编译。IGNORECASE 只影响英文分支，中文无大小写。
_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    op: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for op, pats in _ALL_PATTERNS.items()
}

#: guard 能识别的全部 op。cli 的 `--list-ops` 和测试都用这个。
KNOWN_OPS: tuple[str, ...] = tuple(sorted(_ALL_PATTERNS))

_SNIPPET = 60


@dataclass(frozen=True)
class GuardFinding:
    """扫出的一条。keep evidence：人要能看出是哪句话触发的闸门。

    没有证据的自动升级最招人烦 —— 用户看到 "本任务判 D 类" 却不知道
    自己哪句话说错了，下一步就是想办法关掉 guard。所以 trigger 必须留。
    """

    op: str
    trigger: str          # 命中的原文片段
    pattern: str          # 命中的模式，便于改词表时定位

    def line(self) -> str:
        return f"{self.op}  ←  {self.trigger!r}"


def scan_ops(text: str) -> tuple[GuardFinding, ...]:
    """扫描自由文本，返回命中的 op。同一个 op 只留第一处证据。

    不做否定判断：「不要上线」也会命中 prod_deploy。这是故意的 ——
    要正确处理否定就得做语义分析，而语义分析正是这里不敢依赖模型的原因。
    多报一次由人一眼否掉，成本远低于漏报。
    """
    found: dict[str, GuardFinding] = {}
    for op, pats in _COMPILED.items():
        for pat in pats:
            m = pat.search(text or "")
            if m is None:
                continue
            start = max(0, m.start() - 12)
            snippet = (text[start:m.end() + 12] or m.group(0)).strip()
            found.setdefault(
                op,
                GuardFinding(op=op, trigger=snippet[:_SNIPPET], pattern=pat.pattern),
            )
            break
    return tuple(found[op] for op in sorted(found))


def harden_ops(
    text: str, declared: tuple[str, ...] | list[str] = ()
) -> tuple[tuple[str, ...], tuple[GuardFinding, ...]]:
    """取模型声明与 guard 扫描结果的并集。

    返回 (ops, findings)。findings 只含 guard 新加的那些 —— 模型已经报了的
    不算 guard 的功劳，报表里混在一起会让人看不出 guard 到底拦下了什么。

    并集而非二选一：
      模型报了 guard 没扫出的 → 保留（模型能读懂 "把老数据处理掉" 这种绕弯的说法）
      guard 扫出模型没报的     → 加上（模型漏判不能成为闸门的旁路）
    """
    have = tuple(dict.fromkeys(str(o) for o in declared if str(o).strip()))
    findings = tuple(f for f in scan_ops(text) if f.op not in have)
    return have + tuple(f.op for f in findings), findings
