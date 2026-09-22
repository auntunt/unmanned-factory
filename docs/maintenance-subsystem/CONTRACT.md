# webuddy 运维维护子系统 · 共享契约 `maintenance-subsystem/1`

CLI、HTTP、页面操作的是同一组对象；业务状态全部由后端派生（`factory/control/maintenance_subsystem.py`），
页面与画布不持有状态。已有的 `/api/v2/maintenance/tasks*`（任务、事件、回答、批准、恢复、取消、导出）
保持不变并继续是任务现场的数据来源；本契约只新增代码库、需求接入、监控投影与宿主清单。

对象身份：
| 对象 | ID | 来源 |
|---|---|---|
| 代码库 | `project_id` | 既有 `projects` 表，不另建实体 |
| 需求 | `requirement_id` | `maintenance_requirements` 表（本子系统唯一新增的业务实体） |
| 任务 | `task_id` | 既有 `maintenance_tasks`（issue_maintenance） |
| 执行 | `execution_id` | 既有 `runs` |
| 产物 | `artifact:<task_id>:<name>` | 既有导出（补丁） |

## 监控
`GET /api/v2/maintenance/overview?project_id=<可选>`
```jsonc
{
  "contract_version": "maintenance-subsystem/1",
  "generated_at": "ISO8601",               // 数据时间
  "window": {"kind": "day", "start": "...", "end": "...", "timezone": "Asia/Shanghai"},
  "scope": {"project_ids": ["..."]},       // 仅当前身份有权的已接入项目
  "sources": {                              // 每个数据源的真实连接状态
    "maintenance":   {"status": "connected"},
    "executor":      {"status": "online|offline|unknown", "detail": "..."},
    "server_metrics":{"status": "not_connected", "detail": "未配置采集源"},
    "app_probes":    {"status": "not_connected", "detail": "未配置探针"},
    "alerts":        {"status": "not_connected", "detail": "未配置告警源"}
  },
  "counts": {
    "projects": 3,                           // 有权限的已接入项目
    "running": 1,                            // 活跃任务去重（received/running/cancelling）
    "attention": {"total": 2, "answer": 1, "approval": 0, "blocked": 1},  // 按任务去重，一项只计一次
    "delivered_in_window": 0                 // 产物就绪（非部署）；无数据为 null
  },
  "attention": [{"task_id","project_id","project_name","title","kind":"answer|approval|blocked","reason","since"}],
  "projects":  [{"project_id","name","repository","repo_state","repo_state_label","running","waiting","delivered","delivery_target","service_status":"not_connected"}],
  "events":    [{"task_id","project_id","execution_id","sequence","kind","label","at"}],
  "queue":     {"pending": 0, "running": 0, "executor": {"status": "...", "detail": "..."}},
  "availability": {"state": "enabled|..."},
  "partial_errors": [{"section": "events", "message": "..."}]  // 局部失败不让整页失败
}
```
`attention.kind`：`answer` = 模型提问待业务回答（needs_clarification），`approval` = 计划待批准（awaiting_approval），
`blocked` = 执行/环境受阻（needs_human、failed）。

`GET /api/v2/maintenance/graph?project_id=<可选>` — 同一数据的关系投影
```jsonc
{"generated_at": "...", "nodes": [{"id": "repo:<pid>", "type": "repo|requirement|task|artifact|target|blocker",
  "label": "...", "sublabel": "...", "status": "...", "task_id": "...|null", "project_id": "..."}],
 "edges": [{"from": "repo:..", "to": "req:..", "kind": "has|creates|produces|targets|blocked_by"}],
 "truncated": false}
```
节点 ID 稳定：`repo:<project_id>`、`req:<requirement_id>`、`task:<task_id>`、`artifact:<task_id>:<name>`、
`target:<task_id>`（未交付的目标，不计入已交付）、`blocker:<task_id>`。不为日志建节点。

## 代码库
- `GET /api/v2/maintenance/repos` → `{"repos": [RepoView]}`
- `POST /api/v2/maintenance/repos` `{"source": "<git URL 或 执行主机上的目录>", "name": "...", "branch": "可选", "credential_ref": "可选"}`
  → 201 `RepoView`（已登记的同一仓库返回既有 `project_id`，`reused: true`）。分析在后台进行。
- `GET /api/v2/maintenance/repos/{project_id}` → `RepoView` + `requirements`、`tasks`
- `POST /api/v2/maintenance/repos/{project_id}/probe` → 重新分析
- `POST /api/v2/maintenance/repos/{project_id}/checks` `{"adopt": ["<建议检查名>"]}` → 采纳探测建议的检查命令（管理员）

```jsonc
RepoView = {
  "project_id", "name", "repository", "workspace", "base_branch", "reused": false,
  "state": "pending|analyzing|needs_input|ready|failed",
  "state_label": "待分析|分析中|待补充|可开始维护|接入失败",
  "probe": {"at", "head_sha", "branch", "remote", "access": {"ok", "message"},
            "stack": [{"name", "evidence"}],                 // 已发现，不等于已验证
            "suggested_checks": [{"name", "argv", "evidence"}],
            "findings": [{"id", "label", "status": "found|verified|missing|failed", "message"}]},
  "needs": ["确认检查命令", ...],                            // 待补充项
  "memory": {"entries": 0, "confirmed": 0, "code_index": "on_demand"},
  "credential_ref": null
}
```
「可开始维护」只表示工作区可用、基线可解析、至少一条检查已配置；不代表构建、部署与业务检查通过。

## 需求接入（人工与机器共用 `Intake.submit`）
- 人工（会话登录）：`POST /api/v2/maintenance/requirements` `{"project_id", "content", "attachments": [{"name","ref"}]?}`
- 机器（无会话）：`POST /api/v2/maintenance/intake`，`Authorization: Bearer <接入令牌>`
  `{"project_id", "content", "external_id", "title"?, "attachments"?}`。来源名取自令牌，请求体中的来源/角色字段一律忽略。
- 列表：`GET /api/v2/maintenance/requirements?project_id=<可选>` → `{"requirements": [Requirement]}`
- 派发：`POST /api/v2/maintenance/requirements/{id}/dispatch`（机器来源未开启自动执行时由人派发）
- 接入来源（管理员）：`GET/POST /api/v2/maintenance/intake-sources` `{"name", "project_ids": [...], "auto_dispatch": false}`，
  创建时一次性返回令牌明文，库内只存哈希；`POST /intake-sources/{id}/revoke`。

幂等：键 = (来源, 项目, external_id)。人工提交无 external_id 时由客户端给 `idempotency_key`，否则每次视为新需求。
同键同内容 → 返回原记录（`duplicate: true`，HTTP 200）；同键不同内容 → 409，不覆盖。

```jsonc
Receipt = {"requirement_id", "status": "dispatched|pending_dispatch|dispatch_failed",
           "duplicate": false, "task_id": "...|null", "execution_id": "...|null",
           "clarification": {"state": "analysis_pending|questions|not_needed", "questions": []},
           "received_at", "source": {"kind": "manual|api|cli", "name"}, "message": "已接收，不等于已执行"}
```

## 任务现场补充
- `GET /api/v2/maintenance/tasks/{id}` 追加 `actions`: 后端允许的动作子集
  `["answer","approve","supplement","resume","cancel","feedback","export"]`；页面只渲染其中的动作。
  不存在 `pause`：执行器不支持原地暂停，不显示。
- `POST /api/v2/maintenance/tasks/{id}/feedback` `{"content"}`：已交付/失败后的后续反馈 → 新修订任务（`predecessor_id` 关联），返回新任务视图。

## 宿主清单
`GET /api/v2/maintenance/manifest` → 组件名、版本、契约版本、入口（UI 路由、CLI 命令）、支持的动作、
`supports_pause: false`、宿主需提供的端口（身份、项目权限、执行器配置、数据目录）、嵌入方式。

## CLI（同一核心，`webuddy-maintenance`）
见 [INSTALL.md](INSTALL.md)。两种接法：
1. 本地数据域：`--data-dir`（与 webuddy 服务共用同一 control.db 时看到同一对象）。
2. 最小运行时：`webuddy-maintenance runtime` 在无网页的情况下持有执行器锁并真实执行队列。
