# 功能优先一轮：接线盘点与文件归属

基线 `d6759ea`（工作树 operations-20260921，分支 codex/operations-20260921）。
本页是主会话唯一一次盘点，冻结接口后 A/B/C 并行；不再做第二轮设计。

## 现状：缺的只有一层

M3 已经交付的，**直接复用，不重写**：

| 已有 | 位置 | 说明 |
| --- | --- | --- |
| 维护任务领域端口 | `factory/control/issue_maintenance.py` | `MaintenanceTasks`：create / revise / get / list / events / intervene / resume / cancel / export |
| webuddy 适配与整套接线 | `factory/control/issue_maintenance_webuddy.py` | `tasks_for(svc)` 从活的 Service 直接组出全部端口 |
| 独立 CLI | `factory/control/maintenance_cli.py` | 同一 Task 契约，无 UI 依赖 |
| 职能包目录与加载 | `factory/control/agent_packs.py` + `builtin_packs/` | `catalog()` 按 `packs/*/pack.json` glob，新增目录即自动进目录 |
| 应用外框与导航 | `frontend/src/conversation/{AppShell,nav-config}.tsx/ts`、`frontend/src/App.tsx` | 工程总览页签组已存在 |

**唯一缺口：没有任何 HTTP 路由。** `app.py` 里没有 maintenance router，
`tasks_for(svc)` 至今只被 CLI 用过。F1 卡在这一层，不是卡在领域逻辑。

## 冻结的 HTTP 契约（A 实现，B 按此开发，交付前必须连真实 API）

前缀 `/api/v2/maintenance`。身份沿用既有会话与 `WebuddyIdentity`/governance，
不新增权限模型。任务视图**就是** `MaintenanceTasks._view` 的现有结构，不另造 DTO：

```
task view = {schema_version, task_id, revision, predecessor_id, successor_id,
  status: received|running|waiting|cancelling|delivered|failed|cancelled,
  issue{source,external_id,version,title,body}, issue_digest, project_id,
  baseline{repository,base_sha,base_branch_label}, agreement{revision,skill_version},
  expected_behaviour, delivery_goal, delivery_tier, synthetic,
  execution_id, cost_usd, blocking_reason{kind,message,event}|null,
  steps[{name,state}], delivery{commit,checks[{name,passed,exit_code,reused,
  identity_fingerprint}],unverified,working_copy_base_sha,repository,
  capability_source,synthetic}|null, receipts[], created_at}
```

| 方法 | 路径 | 入参 | 出参 |
| --- | --- | --- | --- |
| POST | `/tasks` | 维护请求体（见 `normalize()` 必填项） | 201 task view |
| GET | `/tasks?project_id=` | — | `{tasks:[view]}` |
| GET | `/tasks/{task_id}` | — | view |
| GET | `/tasks/{task_id}/events?after=` | — | `{events:[{sequence,kind,payload,at}]}` |
| POST | `/tasks/{task_id}/follow-up` | `{content}` | `{recorded,applied,...}` 真实回执 |
| POST | `/tasks/{task_id}/resume` | — | view |
| POST | `/tasks/{task_id}/cancel` | — | view |
| GET | `/tasks/{task_id}/export` | — | `{receipt,text,artifacts:[{name,size}]}` |
| GET | `/tasks/{task_id}/artifacts/{name}` | — | 补丁字节流（附件下载） |

错误：`Conflict` → 409，`KeyError` → 404，`ValueError` → 422，未授权沿用既有 403。
这些映射 `app.py` 已有 handler，不新建异常体系。

## 文件归属（互斥，谁都不碰别人的）

| 执行者 | 独占文件 | 明确不碰 |
| --- | --- | --- |
| A 后端 | 新建 `factory/control/maintenance_routes.py`；`factory/control/app.py` 只加一行 include_router；新建 `tests/test_maintenance_routes.py` | 前端全部、`builtin_packs/`、issue_maintenance*.py 的语义（只调用） |
| B 前端 | 新建 `frontend/src/workbench/Maintenance*.tsx` 及其 `.test.tsx`；`frontend/src/App.tsx`；`frontend/src/conversation/nav-config.ts` | 任何 Python、`builtin_packs/` |
| C 职能包 | `factory/control/builtin_packs/packs/<新包>/`、`builtin_packs/modules/<新模块>.json` | 任何 .py、前端、已有包目录的语义改写 |

`catalog()` 是 glob，所以 C 新增包目录不需要改任何公共文件，A/C 不会撞车。

## 三个用户结果的验收口径

- **F1**：网页建任务 → 真实服务领取 → 详情看到真实进度/阻塞 → 补充有真实回执 →
  按真实状态取消/继续 → 下载补丁与回执 → 刷新仍是同一个任务。
  允许合成仓库与假模型执行器，但 HTTP、持久化、队列领取、界面必须真接线。
- **F2**：目录里能找到并选用 Issue 维护 / 存量技术升级 / 接口适配三个主包，
  以及项目定位 / 行为验证 / 受控交付三个共享方法；要有挂载到正确版本的证据。
- **F3**：运行/等待/失败/已交付有真实区别；健康证据标当前/过期/未检查；
  补充标已记录/待应用/已应用；无可靠分母时只显示阶段，不编百分比。
