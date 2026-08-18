"""对话式控制台：给「提需求的人」用的入口。

和 dashboard.py 的分工 —— 两个都留着，因为服务的是两种人：

  dashboard  维护者视角。合并率、闸门命中、每次尝试的 diff hash、成本分摊。
             回答「这套系统健康吗、哪道闸门在误杀」。
  console    提需求的人视角。我说一句话，然后它现在到哪一步了。
             回答「我那个需求怎么样了」。

把两者合成一个页面试过，结果是首屏一半在讲「分母=派发过」——
提需求的人根本不关心分母是什么，他要知道的是「排到了吗、卡住了吗、为什么卡」。
指标表对他是噪音，对维护者是全部。所以分开。

阶段日志不新造数据源，从现成的三处汇总：

  Journal (JSONL)   run_start / dispatch / gate / recover / run_end
  队列目录           inbox / running / done / needs-human / blocked
  审计库             attempt 轮次、监工裁决、花费

三处各自只知道一段，拼起来才是「这个需求经历了什么」。所以这里做的是
翻译 + 归并，不是新埋点 —— 新埋点会和现有的 Journal 语义打架。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from factory.backlog.journal import Journal
from factory.backlog.store import BLOCKED, DONE, INBOX, NEEDS_HUMAN, RUNNING


#: 一个需求从口语到落地要过的几关。顺序即展示顺序。
#:
#: 措辞刻意避开内部词汇：用户说的是「我提了个需求」，不是「我 enqueue 了一个
#: task_attempt」。intake/split/gate 这些键名留给代码，label 给人看。
STAGES: tuple[tuple[str, str, str], ...] = (
    ("intake", "听懂需求", "把你的话转成有判据的任务书"),
    ("split", "拆功能点", "太大的需求切成能独立验收的小块"),
    ("gate", "过闸门", "查判据是否可执行、有没有危险操作"),
    ("queued", "排队等派发", "已入队，等空闲的执行位"),
    ("running", "机器在做", "worker 正在改代码并自测"),
    ("review", "监工判收", "多个监工独立审，任一否决就打回重做"),
    ("landed", "完成", "验收通过，改动已落在分支上"),
)

STAGE_LABEL = {k: label for k, label, _hint in STAGES}
STAGE_HINT = {k: hint for k, _label, hint in STAGES}

#: 事件的三种成色。ok=过了，wait=正在做/等着，stop=卡住了要人。
OK, WAIT, STOP = "ok", "wait", "stop"


@dataclass
class Step:
    """一条阶段日志。"""

    stage: str
    tone: str
    text: str
    ts: float | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        return STAGE_LABEL.get(self.stage, self.stage)

    @property
    def when(self) -> str:
        if not self.ts:
            return ""
        return time.strftime("%H:%M:%S", time.localtime(self.ts))


@dataclass
class Thread:
    """一个需求的完整经过。console 的展示单位。

    不叫 Task 是因为一个需求可能被 --split 拆成多个 task —— 用户提的是一件事，
    系统里是几条任务。合在一个 thread 里显示，否则用户提一句话看到三张卡片，
    还是搞不清哪个是自己那个。
    """

    thread_id: str
    title: str
    created_at: float
    steps: list[Step] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    #: 原始需求文本。用户复查「我当时是怎么说的」。
    said: str = ""

    @property
    def stage(self) -> str:
        """当前停在哪一关。取最后一条日志的阶段。"""
        return self.steps[-1].stage if self.steps else "intake"

    @property
    def tone(self) -> str:
        return self.steps[-1].tone if self.steps else WAIT

    @property
    def blocked(self) -> bool:
        return self.tone == STOP

    @property
    def done(self) -> bool:
        return self.stage == "landed" and self.tone == OK
