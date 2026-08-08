"""架构监工。spec §4.1 里唯一「出意见」的角色：约定违背、重复实现、死代码。

它拦的是 GitClear 记录的那类 churn / 重复度上升 —— 测试测不出来，
所以这里没有客观裁判，只有干净上下文下的第二双眼睛。

因为它出意见不出证据，两件事必须成立：
  1. 只报能指到具体位置的问题。指不出位置的意见没法打回给 worker 修。
  2. 它的裁决在 dispatcher 里是**软的** —— 见 dispatcher._merge_reports。
     否则一个爱挑刺的监工能把每个任务都拖到三轮上人。
"""

from __future__ import annotations

from collections.abc import Sequence

from factory.audit.models import SupervisorRole
from factory.supervisors.base import SupervisorReport
from factory.supervisors.model_base import (
    ISOLATION_RULE,
    ClaudeJudge,
    assert_intact,
    assert_no_leak,
    fence,
    new_sentinel,
    report_from_call,
)

_MAX_DIFF = 60_000
_MAX_CONTEXT = 40_000

_PROMPT = """你是架构监工。看 diff 和它周边的代码，找出测试查不出来的问题。

只报这四类，其它一律不报：
1. 违背本仓库既有约定（命名、分层、错误处理方式、依赖方向）
2. 重复实现 —— 周边代码里已经有等价的函数或模块
3. 死代码 —— 新增了没有任何调用点的东西
4. 该复用却复制粘贴出来的代码

规则：
- 每个问题必须指到具体文件和函数/行。指不出位置的就不要报，宁可漏报。
- 不报风格偏好、不报「可以更优雅」、不报性能猜测、不重复测试能发现的 bug。
- check 写问题类型加位置（例：「重复实现 factory/x.py:foo」），
  expected 写本仓库的既有做法，got 写这次 diff 的做法。
- 找不到上面四类问题就判 pass，claims 留空。干净的 diff 判 pass 是正常结果。
{isolation}

## 本次 diff
{diff}

## 周边既有代码（判断约定和是否重复实现的依据）
{context}
"""


class ArchitectureSupervisor:
    role = SupervisorRole.ARCHITECTURE

    def __init__(self, judge: ClaudeJudge | None = None) -> None:
        self._judge = judge or ClaudeJudge(model="sonnet")

    def review(
        self,
        *,
        diff: str,
        context: str = "",
        withheld: Sequence[str] = (),
    ) -> SupervisorReport:
        # 和规格监工同一个理由：diff / context 是 worker 完全控制的文本。
        # 这个监工尤其吃亏 —— 它的 context 是**同目录未改动文件**，worker
        # 可以在上一轮把注入内容正常提交进版本控制，这一轮它就成了「既有
        # 代码」，影子代码那道闸门也看不见（它判的是 .gitignore 挡住的文件）。
        sentinel = new_sentinel()
        prompt = _PROMPT.format(
            isolation=ISOLATION_RULE.format(sentinel=sentinel),
            diff=fence(sentinel, "DIFF", (diff or "(空 diff)")[:_MAX_DIFF]),
            context=fence(
                sentinel, "CONTEXT", (context or "(未提供周边代码)")[:_MAX_CONTEXT]
            ),
        )
        assert_intact(prompt, sentinel, 5)
        assert_no_leak(prompt, tuple(withheld))
        return report_from_call(self.role, self._judge.ask(prompt), what="架构监工")
