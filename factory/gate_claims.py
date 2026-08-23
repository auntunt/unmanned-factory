"""闸门 / 故障的领域词典：check 名 → 人话解释。

这两张表原来住在 `factory/dashboard.py` 里，但它们不是 UI 代码 —— 判据是
claim 的 `check` 字段，和渲染无关。旧 dashboard 删掉之后它们继续被新前端和
`factory.metrics` 使用，所以搬到这里独立成模块。

搬迁时表的内容和下面的注释一字未改：那些注释记录的是踩过的坑（为什么两张表
不许合并、为什么不从 dispatcher 源码正则抽取），不是可以精简的说明文字。

本模块**只有数据**，不 import 项目里任何其他模块 —— 这样任何一层（CLI、API、
metrics、前端）都能引它而不引入循环依赖。
"""

from __future__ import annotations

#: dispatcher 里 `_blocked(...)` 的第一个参数全集 —— 也就是「机制闸门」的名字。
#:
#: 这张表**只用于展示分组**：把 claim 分成「机制闸门拦下的」和「监工判的」。
#: 它不参与任何裁决，所以漏一个名字的后果是那条 claim 显示在「其他」组里，
#: 不是漏拦。判决侧一个字都不读这里。
#:
#: 刻意不从 dispatcher 源码里正则抽取：那样做会让展示层依赖另一个模块的
#: 源码写法，dispatcher 改个换行就静默变空。宁可手工维护一张会过时的表，
#: 也不要一张会静默变空的表 —— 前者看得见，后者长得像「这一轮很干净」。
GATE_CLAIMS: dict[str, str] = {
    "diff": "diff 本身为空或读不出来",
    "diff-suppressed": ".gitattributes 把 diff 正文压掉了",
    "fake-green": "金丝雀验出这份绿推不翻",
    "git-config-touched": "worker 动了 .git/config",
    "git-hook-touched": "worker 动了 git hooks",
    "gitlink-added": "新增/改动 mode-160000 的索引条目",
    "head-moved": "HEAD 被移动过",
    "index-skip-flag": "索引跳过标记让改动从 git 眼里消失",
    "info-attributes-touched": ".git/info/attributes 被动过",
    "replace-refs-changed": "refs/replace 让 git 在对象内容上撒谎",
    "runner-hook-added": "新增 runner 自动加载文件（自己出卷子）",
    "shadow-code": "被 .gitignore 挡住的代码文件",
    "spec-criteria-mutated": "worker 改了自己的验收标准",
    "vacuous-checks": "worker 自定的判据恒为真，等于没有验收",
}

#: 不是闸门 —— 是我们这一侧或上游坏了。
#:
#: `harness` 曾经在 GATE_CLAIMS 里，一次真跑批把这件事暴露出来：网关回了个
#: 502，attempt 记成 `reworked` + 红色「闸门 harness」。于是一条**处理得完全
#: 正确**的链路（打回 → 重试 → 合并）在页面上长成「模型写错了、被闸门拦下」。
#: 客户读到的是两件都不成立的事：模型没写错，闸门也没拦任何东西。
#: 同一个形状在 P0 判据上踩过一次 —— 上游抖动不许把正确的链路判红。
#:
#: 分开的第二个理由是那张命中率表：harness 混在里面会让「闸门拦下 N 次」
#: 里掺进一堆 502。拿它做汇报的数就系统性虚高，而虚高的方向是**对我们有利**
#: 的那一侧 —— 这种偏差没人会来纠。
#:
#: 判据仍是 claim 的 check 名，和 GATE_CLAIMS 同一套读法；两张表**不许合并**，
#: 因为合并之后「闸门拦下」和「工具坏了」就只剩一个计数器。
FAULT_CLAIMS: dict[str, str] = {
    "harness": "worker CLI 自己报错（上游 5xx、超时、装的东西不对）",
}
