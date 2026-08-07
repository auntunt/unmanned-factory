"""入口层：自由文本 / 录音 → 结构化 Task。

spec 里入口层是 P1。它的危险处和别的组件不一样：**这是唯一一个由模型
决定任务分级输入的地方**。declared_ops 少写一条，D 类硬闸门就不会触发，
而后面所有环节都不会再有第二次机会发现它 —— 分级引擎只看它拿到的声明。

所以这里的分工是死的：
  extract.py  模型负责把话变成结构（prompt / paths / checks）
  guard.py    确定性关键词扫描负责补 declared_ops，**只增不减**

模型说"没有不可逆操作"不算数；guard 扫出 "上线到生产" 就一定加
prod_deploy。反过来 guard 扫不出的、模型报了的也保留。两边取并集。
"""

from factory.intake.guard import GuardFinding, harden_ops, scan_ops
from factory.intake.extract import DraftTask, IntakeError, TaskExtractor

__all__ = [
    "DraftTask",
    "GuardFinding",
    "IntakeError",
    "TaskExtractor",
    "harden_ops",
    "scan_ops",
]
