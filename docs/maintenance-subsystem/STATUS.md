# 运维维护子系统 · STATUS（2026-09-23）

分支 `codex/maintenance-subsystem`，基于候选 `363da96`（`codex/enterprise-plugins` 当时的最新提交，未回退任何内容）。
候选 SHA 见 [CODEX-HANDOFF.md](CODEX-HANDOFF.md)。

| 交付项（EXECUTE-FULL-SUBSYSTEM） | 状态 | 位置 |
|---|---|---|
| 1 共享业务核心，CLI/API/UI 同一对象 | 完成 | `factory/control/maintenance_subsystem.py`（复用 projects / issue_maintenance / runs / control_jobs） |
| 2 代码库登记与真实探测 | 完成 | URL 或执行主机目录 + 名称；探测版本、访问、技术栈、建议检查；发现≠验证 |
| 3 人工与机器共用接入口 | 完成 | `Intake.submit`；机器走 Bearer 接入令牌，范围=令牌项目，幂等=来源+项目+external_id |
| 4 监控大屏 + 关系画布 | 完成 | `/maintenance`；未接入的数据源标「未接入」，断线保留旧数据并标过期 |
| 5 调查/澄清/批准/真实修改/检查/产物/反馈/失败接续 | 完成 | 复用既有执行器；无暂停；规划前受阻与交付后反馈均生成关联修订 |
| 6 CLI 与两种运行方式 | 完成 | `webuddy-maintenance`；`runtime` 为无网页最小运行时；[INSTALL.md](INSTALL.md) |
| 7 AppShell 内页面 + 嵌入边界 | 完成（嵌入仅验证到路由/响应头） | `MaintenanceShell`、`EmbeddedMaintenance`、`/embed/maintenance/*`、`FACTORY_EMBED_ORIGINS` |
| 8 版本、配置样例、数据目录、启停、事件与产物规范 | 完成 | [INSTALL.md](INSTALL.md)、[config.example.env](config.example.env)、[CONTRACT.md](CONTRACT.md) |

真实验收（合成仓库，真实模型 claude-sonnet-5，用户自有中转配置）：人工 1 条 + 机器 1 条提交 → 监控定位 →
任务现场 → 执行 → 补丁产物 → 交付后继续反馈 → CLI 与页面查询同一对象；干净环境独立运行时再跑通 1 条。
模型总花费约 $5.6（1.24 + 0.36 + 1.45 + 2.55）。证据在 [evidence/](evidence/)。

验收暴露并已修复 9 个问题（均有测试钉住，详见提交 `53c7194`、`19a3f2d`、`4ce5934`）。
