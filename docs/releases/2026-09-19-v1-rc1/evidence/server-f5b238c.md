# webuddy 服务器真实验收 · f5b238c

日期：2026-09-19。测试执行者：Codex。生产服务未更新，仍4c56632。最新候选在同一台Linux服务器的独立工作区运行；模型、GitHub、文件下载及隔离工具为真实调用，未使用DummyRunner。

## 结果一览

| 测试 | 结果 | 证明到哪里 |
|---|---|---|
| Linux修复回归、Skill装载/权限、工具包 | 32 passed / 20.80s | 当前候选在Linux上的这些定向路径通过，不是全量 |
| 平台内真实工具发布/挂靠/调用/下载，执行包下载校验与平台外CLI | 2 passed / 4.66s | 真bwrap隔离和实际文件执行；工具开发源由测试fixture准备，不冒称模型生成 |
| 管理员配置对话 | 通过 | 实际Claude模型将max_parallel从2改为1，表单同源GET读到1，revision1→2，消息completed且无pending；只改隔离库 |
| 私有GitHub Skill导入 | 通过 | 真私有仓库匿名404，授权导入成功，绑定到确定commit，正文hash匹配 |
| 读取团队规则→报价→文件导出 | 内容与产物通过，界面完成状态未通过 | 真实模型使用未在用户提示中提供的0.87折扣和正文专属随机校验号，99.90×3×0.87=260.74，Markdown下载成功；无开发run生成 |
| 跨用户与管理员入口边界 | 通过 | 无关member读取他人会话Skill403、进入配置对话403 |
| 服务真重启后持久化 | 通过 | 消息、Skill绑定、导出记录仍可获取 |
| 旧开发现场恢复 | 通过 | 原needs_human现场在最新候选人工continue一次后约81秒到ready_for_review；counter的加/减/reset独立实际运行通过 |
| GitHub成果发布 | 经一次人工缓存处理后通过 | 平台正常发布至私有仓库，GitHub远端SHA与验收提交相同；不算无人工完整交付 |
| 普通member开始日常聊天 | 未通过 | 正向路由最小复现403：全局中间件未放行普通用户的do会话创建 |
| 固定公司测试地址与同址更新 | 尚未执行 | 实际测试域名/目标尚待用户确认；没有占用平台域名冒充应用测试地址 |

## 真实环境与运行

- 候选：f5b238c3aadc379e760d6a8624589bb6caa46b45。
- 工作区：/home/ubuntu/releases/audit-next-f5b238c。
- 新鲜独立数据库、loopback真实uvicorn服务18919端口；不是TestClient预览夹具。
- 模型配置：claude / claude-sonnet-5。按现有服务身份调用SDK；没有打印或复制API token/SSH私钥。
- 管理员配置job：a9dc94d66fe74d60a4762c5a6fd6ead4。
- 报价job：2c641469e9e740e5874694796e4eb7b3。
- 私有Skill测试仓库：auntunt/webuddy-acceptance-20260919-172853。
- Skill commit：21a8555dabfffc9dac20a482f17f6478d95aa20c。
- 报价文件见 quote.md。它只含合成业务资料。
- 真实工具导出有服务器持久化文件与可下载结果；此次探针未另保存calc工具调用事件明细，因此报价数值正确和文件生成不能替代逐次calc事件审计。

## 普通聊天两处缺口已直接交给Claude

1. job完成后还遗留同job的pending，前端messages.some决定继续typing与轮询。真实证据见live-acceptance.json和chat-status-finding.md。答案本身和文件正确，不等于页面完成状态正确。
2. 普通member无法开始自己的无项目do会话，403管理员权限。见test_member_daily_chat.py与member-chat-finding.md；本地HTTP路由复现1 failed / 1.17s。尚不是浏览器实际点击验证。

分别通过Claude Desktop「WebUddy Next 启动开发」Message11、Message13派发。第二条发送时为unread队列；未宣称它已修复。限定已有聊天生命周期与窄权限接线，不扩全局权限、不做复杂group、不重跑后端全量。

## 开发现场与GitHub发布的真实边界

恢复的是此前保留的Counter现场，不是重新生成SaaS。旧验收报告保留不改；执行前备份独立验收库到服务器受限文件，不包含在本地证据中。人为continue一次，不能说此轮无人自动恢复。

成果提交8d64c54afcc16b2f4ca152e6054168c932626f2a，Counter加/减/reset业务检查exit0。

首次平台发布返回400“验收后工作区已变化，拒绝发布”。核对HEAD与验收SHA相同，无源码差异，仅未跟踪__pycache__/。将该缓存目录移到证据工作区保存（没有删除或豁免任意业务文件），改用python -B复核业务且确认git干净，再通过同一平台bound发布入口重试；复用已有仓库，没有重复创建。

私有成果仓库：auntunt/webuddy-acceptance-counter-b0407128。
远端default branch提交与验收SHA一致，平台status=published。证据见github-retry-report.json。

这证明恢复、验证和GitHub发布可工作，但包含人工缓存处理；发布前生成文件导致脏工作区的体验可作为后续小项。未绕过工作区检查、未篡改验收状态、未强推。

## 仍未宣布通过

- 普通成员完整聊天、完成后停止轮询：等待Claude修复候选与复验。
- SaaS固定地址真实核心操作与第二轮同址更新。
- 最新前端整套浏览器视觉验收。
- 完整MFD保真转换、公司PPT等业务工具，仍是单独需求。
- 新版生产发布；当前生产仍旧版。

## 证据文件

- live_acceptance.py / live-acceptance.json：隔离HTTP、真实SDK、私有GitHub、报价、边界与重启。
- coding_recovery.py / coding-recovery-report.json：旧现场继续、真实验证与首次发布失败。
- github_retry.py / github-retry-report.json：缓存保留处理后的正常发布与远端SHA比对。
- quote.md：实际下载的报价文件。
- chat-status-finding.md / member-chat-finding.md / test_member_daily_chat.py：已交Claude的缺口与复现。

本报告将成功、失败、人工处理和未测项分开记录，不把全部检查合并成“平台全绿”。
