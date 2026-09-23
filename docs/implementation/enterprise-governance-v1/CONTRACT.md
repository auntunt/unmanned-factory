# 企业治理 v1 契约：组织树、项目归属、负责人只读管理范围

范围是最小纵切，不是完整 IAM。设计方向见 `docs/design/enterprise-governance-console.md`；本文件只写**已实现**的部分。

## 现状缺口（开工前核实）
- 账号只有全局 `admin` / `member`（`users.role`）；成员靠 `team_projects` 获得项目**执行**权（`Governance._can_run`）。
- 所有非 GET 请求默认只有 admin 能发，成员只有中间件白名单里的动作（`app.py` 中的 `boundary`）。
- **既有通用 GET 接口对所有登录账号开放**（例如 `/api/v2/projects`、`/api/v2/runs`、`/api/v2/runs/{id}`、`/events`、`/conversation`），代码注释说明这是 "shared workspace"。本轮**没有改**这些接口：一来保证成员工作台不退化，二来收紧读权限是单独的产品决定。所以，组织范围约束的是 `/api/v5/management/*` 这个管理视图，**不是**跨部门的保密边界。见“限制”。

## 数据（存放在 users.db，与 team_projects/team_audit 同库；只新建表，不改旧表）
- `org_units(id, name, kind∈company|department|group, parent_id, created_at)`：唯一顶层（部分唯一索引），其余节点必须有上级。
- `org_projects(project_id PK, unit_id)`：一个项目最多归属一个组织。
- `org_scopes(user_id, unit_id, granted_by, granted_at)`：只授予 `management.read`，覆盖该节点整棵子树。
- 审计：复用只追加的 `team_audit`，动作前缀 `org.`，内容经 `scrub()` 脱敏。

## 有效可见性（唯一判定：`OrgGovernance.visibility`）
- admin：看全部组织和全部项目，包括未归属的旧项目。
- member：把每个授权节点的子树并起来，可见项目 = 归属于这些节点的项目。未归属项目永远不会进入负责人的范围。
- 停用账号：没有任何范围。
- 每次请求都从表里重新计算，所以撤销授权、移动节点、改项目归属都在**下一次请求**生效。

## 接口
| 方法 | 路径 | 谁能用 | 说明 |
|---|---|---|---|
| GET | `/api/v5/me/workspaces` | 登录用户 | `{management, admin, scopes[], execution_note}`，决定是否显示“管理”入口 |
| GET | `/api/v5/org` | admin | 组织树、项目与归属、未归属项目、授权、用户列表 |
| POST | `/api/v5/org/units` | admin | `{name, kind, parent_id}`；第二个顶层 409；父节点不存在 404 |
| PATCH | `/api/v5/org/units/{id}` | admin | `{name?, parent_id?}`；移到自己或下级之下 422；移动顶层 422 |
| DELETE | `/api/v5/org/units/{id}` | admin | 还有下级、项目或授权时 409 |
| PUT / DELETE | `/api/v5/org/projects/{project_id}` | admin | 绑定 `{unit_id}` 或解绑；项目或组织不存在 404 |
| POST | `/api/v5/org/scopes` | admin | `{user_id, unit_id}`；用户或组织不存在 404；对象是 admin 或已停用 422；重复授权 409 |
| DELETE | `/api/v5/org/scopes/{user_id}/{unit_id}` | admin | 撤销 |
| GET | `/api/v5/management/overview?unit_id=&days=30` | admin 或有范围的成员 | 见下 |
| GET | `/api/v5/management/projects/{project_id}` | 同上 | 项目任务摘要（最多 100 条） |

没有范围的成员访问 management 接口 → 403。范围外的 unit_id / project_id 与不存在的 ID 返回同一个 404（`范围不存在或你无权查看`），不泄露它是否存在。写接口对非 admin 有两层拦截：中间件默认拒绝，路由里再检查一次。

## overview 字段
- `scope`：`label`、`units[]`（范围内的节点）、`grants[]`。
- `window`：`days`（1–90）与 `since`。进行中和待处理按**当前**状态统计；成果就绪、已发布、失败只统计窗口内更新过的任务。
- `generated_at`：数据生成时间。
- `counts`：`in_progress / pending / ready / published / failed`。**先按可见项目过滤，再计数。**
- `projects[]`：`name`、`unit_path`、`counts`、`initiators`（任务记录里的发起人；项目本身没有负责人字段，不推断）、`last_activity_at`。
- `pending[]`：真实任务的状态和原因，外加 `can_act`，其判定与既有中间件相同（admin，或者是发起人且有项目执行权）。不能操作时的提示是“需要发起人或管理员处理”。**不存在**预算审批或发布审批队列。
- `usage`：费用来自运行事件 `usage.recorded`。没有记录时 `recorded=false`、`known_cost_usd=null`（显示“未记录”，不显示 0）；另外单独给出未知费用的调用数。token 数取本月 `token_calls` 台账。
- `audit[]`：只包含触及范围内节点或项目的 `org.*` 记录，字段有操作者、动作、时间、`data.unit_path`/`target_username`/`result`。
- admin 在不指定 unit 时，额外返回 `unassigned_projects`。
- 管理视图**不返回**对话、原始请求全文、日志或凭据。任务标题取方案标题，没有方案标题时退到请求文本，截到 80 字。

## 无损升级
启动时只执行 `CREATE TABLE/INDEX IF NOT EXISTS`，不迁移、不改写旧表。旧的 admin/member、team_projects、配额和审计保持原样。组织树为空时，只有 admin 能看到管理面（包括所有未归属项目）。
