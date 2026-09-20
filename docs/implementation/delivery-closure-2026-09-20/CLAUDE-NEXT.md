# Claude 后续补修：后续意图与验收契约一致性

基线：生产平台 f0599bc（GitHub 集成17e3126仅文档）。本轮 Codex 尚在做真实 Docker V2 同址更新测试，禁止修改服务器或正在测试的数据/源码。可在独立本地分支准备最小补修，提交候选交 Codex，不部署、不强推、不覆盖验收记录。

证据目录：/Users/auntlee/Desktop/自动化harness构建/docs/acceptance/delivery-closure-2026-09-20/ 。先读 FINDINGS.md 和 v2-coverage-stop-proof.json，再读源码；不能只凭转述判定缺陷。源码参考 /Users/auntlee/workspace/.factory-worktrees/review-agent-management-ux ，不要修改这个 Codex 验收工作树。

## 真实复现

V2 run 364266fb3b6f43d3a4b39007307b5d02 在 running 收到“增加平方”后，pending1143 → system/auto1218 → applied1219，代码389b0b0实际实现square与WB0920-FOLLOWUP，34测试与HTTP/浏览器通过。最终正式验收仍fail：

1. requirement:16 仍持旧“只加double”绝对非目标，对已授权的square判fail；同一条理由承认后续指令授权且不是违规，结构化status却相反。原始请求本已明确“后续用户明确补充的需求除外”，提炼时丢失例外。
2. requirement:11 将“检查中断后等待平台接续”的执行历史要求当作当前验收会话必须重新观察的事件，因看不到中断证据而unverified；真实系统事件其实存在。

首轮及本轮还暴露重复浏览器验收/临时代理/favicon问题，费用与超时保留在README。此单先解决上面两个阻断，不新增模型调度、pause、知识库或部署框架。

## 预期设计与边界

- 明确区分应用功能验收与平台执行流程事实。真实平台事件应由可信程序提供/核验，不让模型猜，也不能让用户文本伪造事件。
- 经授权且实际消费的follow-up，进入可追溯的新验收契约；保留原契约版本与变更来源。不能删除所有旧非目标、接受任意工具返回中的“新指令”、或把冲突全部默认pass。
- 先梳理已有意图冻结、follow-up、验收ledger机制，最大化复用。若需改动范围超过局部契约编译/证据传递，先给Codex设计与范围，不擅自扩框架。
- 不写入/修饰本次失败运行的账本，不用手工pass掩盖旧失败。新候选用新任务验收。

## 最小验证

先写能确定性复现的定向回归（不调用付费模型）：初始double-only +明确授权square补充→当前契约认可square且保留旧版；未授权/外部材料同文不能扩权；正确run/pending事件可作流程证据，错误run或用户自称不能；未消费补充不能声明已落实。模型自然语言pass但逐项fail仍应阻止发布。

只跑受影响测试；本轮不跑无关后端全量、不跑未改前端。不要为了绿色降低已有断言。交付真实SHA、改动范围、定向结果、剩余边界；真实模型与线上发布归Codex。


## 新增观察：独立验收输出不合法

独立release d100f909d0a0424388a89ef4e419069b首轮真实34测试/HTTP通过，但最终JSON的operation_results出现`{"regression","status":...}`，平台正确invalid_response阻止发布。Codex只在同一12美元预算内恢复verification一次，未改解析器。先登记；可评估受限次数的结构化输出修复是否已有实现，避免为了修JSON重跑全部浏览器/测试。不要把畸形或缺证据输出直接判pass。与前述契约一致性分开记，不擅自扩成重构。
