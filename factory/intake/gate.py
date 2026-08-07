"""入口闸门：一份草稿够不够格自己进队列。

在这之前，「无人」只覆盖了派发之后 —— 任务从哪来始终是人写 YAML 或者人跑
`factory prd` 然后人看一眼。这个模块决定的是那个「人看一眼」在什么条件下
可以不做。

DraftTask 的文档说得很直白：入口层的产物必须先落成 YAML 给人看一眼，
「直接从一段话接进 dispatcher 就等于把『我想改点东西』变成了自动派发，
分级的输入（paths / ops）也就没人核对过了」。这个顾虑是对的，所以闸门
**不是**把它取消，而是把它变成可判定的条件：

  人看一眼是为了发现「这条任务的分级输入不可信」。
  那就把「不可信」的特征列出来，命中任何一条就不许自动进队 —— 落到
  needs-human，等人。剩下的才自动进 inbox。

关键取舍：**闸门宁可拒绝也不放过**。被误拒的草稿代价是一次人工确认，
和现状一样；被误放的草稿代价是花钱跑了一个没人核对过分级输入的任务。
两边不对称，所以每条规则都往拒绝的方向倒。

它**不做**分级。分级是 GradingEngine 的事，而且 dispatcher 里还会跑两次
（派发前按声明、拿到 diff 后按实际）。闸门在分级之前，判的是「这份声明本身
值不值得拿去分级」。C 类永不无人、D 类硬闸门这两条边界完全不经过这里 ——
它们在 dispatcher 里，闸门放行的任务照样要过。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 声明了不可逆操作的草稿一律不自动进队。理由不是「D 类危险」（那是 dispatcher
# 的判断），而是：不可逆操作的声明是**模型从自由文本里抽出来的**，抽错的方向
# 有两种，其中一种没有下游能兜住 —— 少抽了。少抽一个 prod_deploy，分级就看不见它。
_OPS_REASON = "声明了不可逆操作，抽取可能漏项，分级输入必须人核对"


@dataclass(frozen=True)
class Admission:
    """能不能自动进队，以及不能的原因。

    reasons 是给人看的：草稿会带着它落到 needs-human，人打开就知道
    差什么。空 reasons 才算放行 —— 不用额外的布尔字段，避免两者不一致。
    """

    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def admitted(self) -> bool:
        return not self.reasons


def admit(draft) -> Admission:
    """判一份 DraftTask 能否自动进 inbox。

    只读 draft 的字段，不碰文件系统、不调模型 —— 这样它能被穷举测试，
    而一个决定「什么可以无人跑」的判据不能只有真跑过才知道对不对。
    """
    reasons: list[str] = []
    warnings: list[str] = []

    # 1. 模型的疑问：**只在没有验收标准时**才算拦。
    #
    #    实测出来的：一句写得挺完整的需求（"加个 slugify，验收标准是
    #    slugify('Hello World') == 'hello-world'"，acceptance / checks /
    #    declared_paths 都齐了）照样被抽出两条 unclear —— 连续空格怎么办、
    #    要不要做类型校验。这两条是真的没说，但它们**恰恰是 acceptance 划定
    #    了边界之后不必回答的问题**：验收只要求那一个用例过，多余的行为
    #    worker 怎么定都行。
    #
    #    自然语言永远有没穷尽的边界情形，所以 unclear 一票否决 = 无人路径
    #    永不触发，闸门退化成一个更啰嗦的 `factory prd`。
    #    有 acceptance 时它们降级成提示：验收标准就是那个边界。
    if getattr(draft, "unclear", ()):
        n = len(draft.unclear)
        if getattr(draft, "acceptance", ()) or getattr(draft, "spec_ref", ()):
            warnings.append(f"模型有 {n} 条疑问，但 acceptance 已划定边界，"
                            f"未穷尽的行为由 worker 自定")
        else:
            reasons.append(f"模型有 {n} 条待确认问题，且没有验收标准兜底")

    # 2. 没有 check → 回归监工无从判定。
    #    这条不是「检查缺失」这么轻：没有 check 的任务在回归监工那里拿到的是
    #    no-checks-defined FAIL，三轮全红然后上人。放它进队等于确定烧三轮钱
    #    换一次「这任务没写验收方式」，而这句话现在就能免费说出来。
    if not getattr(draft, "checks", ()):
        reasons.append("没有可执行的 check，进队只会烧三轮再上人")

    # 3. 验收标准为空 → 只有 check 没有标准时，「全绿」的含义取决于 check 写得
    #    多严。人写 YAML 时能自己权衡，自动进队没人权衡。
    if not getattr(draft, "acceptance", ()) and not getattr(draft, "spec_ref", ()):
        reasons.append("acceptance 和 spec_ref 都为空，没有可核对的验收标准")

    # 4. 声明了不可逆操作
    if getattr(draft, "declared_ops", ()):
        ops = ", ".join(draft.declared_ops)
        reasons.append(f"{_OPS_REASON}：{ops}")

    # 5. guard 从原文里额外嗅到了不可逆操作 —— 模型没抽出来但关键词在。
    #    这正是第 4 条担心的「少抽了」的实例，所以它是拒绝而不是提示。
    findings = getattr(draft, "guard_findings", ())
    if findings:
        ops = ", ".join(sorted({f.op for f in findings}))
        reasons.append(f"guard 另外嗅到不可逆操作（模型未声明）：{ops}")

    # 以下是提示，不拦：它们让任务更可能被打回，但都是 worker 修得了的，
    # 或者不影响分级输入的可信度。拦下来只会把闸门变成一个吹毛求疵的东西。
    if not getattr(draft, "declared_paths", ()):
        warnings.append("declared_paths 为空：范围监工不会拦越界改动")

    return Admission(tuple(reasons), tuple(warnings))
