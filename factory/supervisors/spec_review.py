"""规格监工。spec §4.1：输入 = spec + diff，**不给** build log。

「拿不到过程叙述，只能对代码和标准核」—— 所以 worker 的总结、测试输出、
回归监工的结论一律不进 prompt，并由 assert_no_leak 在运行时兜住。

没有验收标准时判 FAIL 而不是 PASS：无标准可核就不是「核过了」。
和回归监工「没有 check 就判 FAIL」同一条道理。
"""

from __future__ import annotations

from collections.abc import Sequence

from factory.audit.models import SupervisorRole, Verdict
from factory.supervisors.base import SupervisorReport
from factory.supervisors.model_base import (
    ClaudeJudge,
    SUPERVISOR_ERROR_PREFIX,
    assert_no_leak,
    report_from_call,
)

_MAX_DIFF = 60_000
_MAX_CONTEXT = 40_000

_PROMPT = """你是规格监工。逐条核对下面的 diff 是否满足每一条验收标准。

规则：
- 只看 diff、被引用的既有代码、验收标准。没有构建日志、没有测试输出、
  也没有作者的说明，这是有意的：你只对代码和标准核，不采信任何过程叙述。
- diff 调用了既有代码时，去下面「被引用的既有代码」里查它的实际实现再判断，
  不要因为「diff 里看不到」就判 fail。
- 任何一条验收标准无法从代码里确认已满足，就判 fail，并为该条给一个 claim。
- claim 的 check 写验收标准的编号或原文，expected 写标准要求什么，got 写 diff 里
  实际是什么。got 不许写「不清楚」这类空话，要指出具体代码或指出缺什么。
- 全部满足才判 pass。pass 时 claims 留空。
- 不要评价代码风格、命名、性能，那是别人的活。只核验收标准。

## 验收标准
{criteria}

## diff
```diff
{diff}
```

## 被引用的既有代码（判断 diff 调用的东西行为是否符合标准）
```
{context}
```
"""


class SpecSupervisor:
    role = SupervisorRole.SPEC

    def __init__(self, judge: ClaudeJudge | None = None) -> None:
        self._judge = judge or ClaudeJudge(model="sonnet")

    def review(
        self,
        *,
        diff: str,
        criteria: Sequence[str],
        context: str = "",
        withheld: Sequence[str] = (),
    ) -> SupervisorReport:
        """withheld 传 build log / worker 总结之类，仅用于运行时校验没漏进去。

        context 给 diff 引用到的既有代码。第一次真跑暴露的问题：任务要求
        「复用已有实现」，worker 照做了，规格监工却因为 text.py 不在 diff 里
        而三轮都判「无法确认返回类型」—— 它的推理没错，是输入不够。
        §4.1 扣的是**构建日志**（过程叙述），不是被引用的源码；
        扣掉源码只会让它变瞎，不会让它变独立。
        """
        if not criteria:
            return SupervisorReport(
                role=self.role,
                verdict=Verdict.FAIL,
                claims=(
                    {
                        "check": f"{SUPERVISOR_ERROR_PREFIX}spec-no-criteria",
                        "command": "",
                        "expected": "至少一条可核对的验收标准（spec_ref）",
                        "got": "任务没有给验收标准，无标准可核不等于核过了",
                    },
                ),
            )

        prompt = _PROMPT.format(
            criteria="\n".join(f"- {c}" for c in criteria),
            diff=(diff or "(空 diff)")[:_MAX_DIFF],
            context=(context or "(未提供既有代码)")[:_MAX_CONTEXT],
        )
        assert_no_leak(prompt, tuple(withheld))
        return report_from_call(self.role, self._judge.ask(prompt), what="规格监工")
