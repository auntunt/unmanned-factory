# 给 Codex 的交接 · 运维维护子系统

- 分支：`codex/maintenance-subsystem`（已推送 origin），基于 `363da96`。候选 SHA 以 `git log -1 origin/codex/maintenance-subsystem` 为准（本文件所在提交）。
- 写入者：Claude（Opus 5.5）统筹并独写共享核心；三个执行子代理请求 `sonnet`，各自报告实际模型为 **claude-sonnet-5**：
  监控+画布页、外壳/代码库/需求/任务现场页、CLI+运行时+安装文档。子代理产出均经集成方复核、改过再提交。
- 未部署、未碰生产服务器与生产数据库；验收只用 scratch 数据目录与合成仓库。

## 实际功能
1. 共享核心 `factory/control/maintenance_subsystem.py`：代码库探测、需求接入、接入来源、监控总览、关系投影、任务动作、反馈修订、宿主清单。新增表仅 `maintenance_repo_probes / maintenance_requirements / maintenance_intake_sources`；项目、任务、执行、事件、队列全部复用。
2. HTTP：`factory/control/maintenance_subsystem_routes.py`（路径见 [CONTRACT.md](CONTRACT.md)）。`GET /api/v2/maintenance/tasks/{id}` 新增 `actions`，HTTP 与 CLI 共用 `maintenance_routes.task_view`。
3. `app.py` 中间件：`POST /api/v2/maintenance/intake` 免会话/Origin，由路由校验 Bearer 令牌；成员可 `POST /api/v2/maintenance/requirements`（路由里做项目授权）；`/embed/*` 仅在 `FACTORY_EMBED_ORIGINS` 设置时改用 `frame-ancestors`，其余仍 `X-Frame-Options: DENY`。
4. 页面：`frontend/src/maintenance/*`，挂在 AppShell 内 `/maintenance`（监控默认）、`/repos`、`/intake`、`/about`；旧列表移到 `/maintenance/tasks`，任务现场 `/maintenance/:taskId` 保持原路由。
5. CLI `webuddy-maintenance`（`factory/control/maintenance_cli.py`，旧子命令保留）+ `runtime` 最小运行时。

## 命令与证据
```bash
uv run pytest -q tests/test_maintenance_subsystem.py tests/test_maintenance_subsystem_cli.py tests/test_issue_maintenance_cli.py tests/test_maintenance_routes.py tests/test_control_app.py -m "not smoke"   # 69 passed
cd frontend && npx vitest run src/maintenance src/workbench/MaintenanceTaskDetail.test.tsx src/workbench/MaintenanceTasksPage.test.tsx src/conversation/AppShell.test.tsx src/conversation/pages.test.tsx   # 83 passed
cd frontend && npx tsc --noEmit -p tsconfig.app.json && npm run build   # 通过（一次构建）
```
- 真实页面截图：`evidence/01..08-*.png`（1440 与 390 宽）。
- 关键事件（已裁剪，无密钥）：`evidence/key-events.json`；机器接入回执（201 → 重复 200）：`evidence/machine-intake-receipts.txt`。
- 产物：`evidence/v2-delivery.patch`、`v3-delivery.patch`（网页服务数据域），`standalone-runtime-delivery.patch`（干净环境独立运行时）。

## 验收中发现并修复的问题（值得独立复核）
1. 探测把脏工作区写成无害备注，而规划器直接拒绝 → 列为待补充项。
2. 规划前受阻的任务既不能 resume 也不能 retry 绑定 → 后续反馈取消旧执行并生成关联修订。
3. 模板交付目标含「部署」，命中规划器高风险词表，所有接入任务被迫人工批准 → 改写并加测试。
4. 反馈生成修订后，已交付计数归零、产物节点消失 → 交付按修订统计。
5. `events --follow` 被旧命令分支截走 → 修复，测试经变异验证。
6. 监控事件流混入 provider.timing 等引擎细节 → 只列业务里程碑。
7. CLI 的只入队 Service 把占位模型 `codex/cli-enqueue` 写进新数据域的 runtime_settings，独立运行时随后用它执行（**此问题早于本分支就存在于旧 CLI**）→ 只从环境变量播种。
8. CLI `show` 与页面阻塞原因不一致 → 共用 `task_view`。
9. 页面：子系统无内容边距、顶栏标题错、需求页溢出、画布节点重叠与未自适应。

## 未验证 / 已知限制
- Codex 执行器、非 macOS、URL 克隆登记的真实网络路径、真实外部宿主 iframe 嵌入、成员角色页面操作。
- 机器来源以「令牌创建者」的项目权限为上限并收窄到令牌项目；创建者被降权后令牌随之失效。
- `synthetic` 标记未从代码库传到接入任务：合成仓库的回执 `synthetic: false`（本次验收仓库在 README 中标注 SYNTHETIC）。
- 项目记忆会把旧交付的文字带进后续提示词；本次 v3 因 v2 记忆里的旧措辞「不部署」仍被判为高风险需批准，新交付不再写入该词。
- 画布同一任务的多个产物依次下排，可能与下一行任务视觉相邻（连线正确）。
- 服务器 CPU/内存、应用探针、告警没有采集源，页面如实显示「未接入」。
- 验收数据域里有一条机器需求任务停在「待批准」，作为监控的真实待处理样例保留。

## 部署影响
- 新增 3 张表（`CREATE TABLE IF NOT EXISTS`，启动时自动建），无迁移、无删改既有数据。
- 新增公开路由 `POST /api/v2/maintenance/intake`（无令牌 401）；反代需放行 `Authorization` 头。
- 新入口脚本 `webuddy-maintenance`；新可选环境变量 `FACTORY_TIMEZONE`、`FACTORY_EMBED_ORIGINS`、`FACTORY_CLONE_TIMEOUT`。
- 前端需重新构建；`/maintenance` 默认页从旧列表变为监控页。
