# webuddy-r1 批次计划（2026-09-17）

规划/监工：Fable 5（宿主报告 model=claude-fable-5）。执行：经宿主 Agent 工具派发 model=sonnet 的子任务（宿主支持模型指定，非角色扮演）。独立验收：Codex（外部，本批次不代写其结论）。

基线：工作区 `/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons`，本地分支 `v3-conversation-workspace`，HEAD `f81622a`，与 `origin/codex/autonomous-factory-v3` 同步，无未提交改动。生产 `4c56632`。S0（职能包复核轮次 2）已收尾并推送，待 Codex Linux 复验。

## 已实现 / 需修复 / 缺证据 / 未实现（首查快照）

- 已实现（需按单补证据）：统一 AppShell 五区页面；run 生命周期 clarify/continue/approve/retry/cancel/discard（run_lifecycle.py）；GitHub 发布含回执（github_publication.py）；部署目标+SSH 远端执行与证据采集（deploy_targets.py / remote_targets.py）；职能包生命周期与 CapabilityPanel（f81622a）；StartChat 服务端分流（/api/v4/route）。
- 需修复（已确认）：S2 缺口——run 处于 ACTIVE 时 follow-up 只存 user.message（applied=false），响应要求用户"再次提交"（run_routes.py 约 176-180 行）；违反 V1-05。
- 缺证据：V1-01～04 真实链路（一句需求→开发→测试→GitHub→固定测试地址→同任务修改→同地址更新）本地段的端到端证据；线上段归 Codex。
- 未实现/未查实：S3 通用 CLI 挂靠的去 MFD 专属化程度；S4 交付类型区分（V1-06）与页面收口细节。F01～F06 明确不做。

## 任务队列（每单一条用户路径）

| ID | 阶段 | 内容 | 状态 |
|---|---|---|---|
| T01 | S1 | 真实链路探针 | local_reviewed |
| T02 | S1 | B1 修复（providers 凭据源）+ 两轮真实穿链 | local_reviewed |
| T03 | S2 | 干预闭环耐久化 | local_reviewed |
| T04 | S3 | 通用工具复用 + csv-quote-xml 真实全链 | local_reviewed |
| T05 | S4 | 交付类型与结果卡收口 | running |
| T06 | S1 | S03 公司测试环境配置区 | local_reviewed |

范围锁定沿用 UIUX-1.0；页面编号见 page-contracts.md；不新增一级入口。
