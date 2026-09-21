# STATUS：企业场景插件化（集成者批次）

分支 `codex/enterprise-plugins`，基线 `b854a41`（远端 `codex/operations-20260921` 的 tip，
即已核对候选；本地原为 `c5dc518`，快进一格，无回退）。

## 批次一：P0 现场核对

- 工作树 `/Users/auntlee/workspace/.factory-worktrees/operations-20260921` 干净、无 git 锁；
  全机唯一在跑的进程是另一个工作树里无关的 Vite dev server，没有别的写入者。
- `git merge-base --is-ancestor HEAD origin/…` 为真 → 快进，不是分叉。
- `b854a41` 只有两个文档文件（FROZEN-MILESTONE.md、STATUS.md 追加），无产品代码变更。
- 文件所有权表见 OWNERSHIP.md，B0 复用裁定见 B0-REUSE.md。
- 文档根工作树曾经的 `4c56632` **没有**被当成基线；编码基线是现场核对过的 `b854a41`。

## 批次二：P1 插件注册与启停（提交 `741a55b`）

**已实现入口**

- `GET /api/v2/plugins`、`GET /api/v2/plugins/{id}`、`POST /api/v2/plugins/{id}/state`、
  `GET /api/v2/plugins/{id}/audit`（写操作由控制面中间件默认限管理员，没有另造一套角色判断）
- `GET /api/v2/maintenance/availability`，以及 `GET /api/v2/maintenance/tasks` 的响应里带可用性
- 界面：设置 → 业务插件（`/settings/plugins`）；维护任务页按可用性决定新建入口并说明原因

**实际产物**

- `factory/control/plugins.py`：固定清单的三个插件声明（无动态加载、无上传）、
  `enabled/draining/disabled` 状态机、CAS + 追加式审计（带 SQLite 触发器）、
  以及 `GatedPort` —— 只暴露归过类的业务方法，未归类的方法直接 `AttributeError`。
- `tasks_for` 成为唯一装配点，`maintenance_cli.py` 改为调用它。
  **这修掉了一个真的会出货的洞**：此前 CLI 自建端口，管理员在网页停用插件后，
  CLI 仍然能派发一次付费执行。
- `approve` 把决定交给运行生命周期而不是端口，单独补了一次闸门。
- 停用必须先排空；排空期间有活跃执行则拒绝，并且明说不会自动取消客户任务。
- 停用后：任务详情、事件、回执导出、补丁下载、取消，全部照常。

**必要检查**

- 后端 133 passed（`test_business_plugins` 22 + 维护全套 + `test_control_app`）
- 前端 33 passed（维护列表/详情/插件设置），生产构建通过
- 6 个针对闸门的变异全部被测试杀死：去掉 tasks_for 的闸门、approve 不问闸门、
  CLI 交出未包装端口、停用不要求先排空、排空忽略活跃执行、可用性读快照而非读行。

## 批次三：B0 项目记忆（提交 `5a29738`）

- **没有新建存储**。`knowledge.py` 的条目已带齐来源/确认状态/适用范围/时间/代码版本。
  `scenario_memory.py` 只补三场景共用的约定：标题前缀分组，以及
  「已确认才进提示词正文，未确认只出标题并标明不作为依据」。
- 维护任务在**派发**时读一次项目记忆随提示词带走——不是在受理时读，
  所以受理之后才被确认的约束对这条任务仍然生效（有测试）。
- 代码关系查询沿用仓库内已接线的 `codegraph.py`，不装第三方图工具（理由见 B0-REUSE.md）。
- 检查：`tests/test_scenario_memory.py` 8 passed；4 个「未确认被当成已确认」的变异全部被杀死。

## 批次四：三个场景（提交 `30f987e`）

三个场景由三个 Sonnet 子会话并行实现，集成者核对后合并。每个场景的端口都照维护
模块的形状做，共用同一个可用性闸门与同一份项目记忆，没有各造一套。

**信创化改造** `legacy_modernization.py` + `modernization_routes.py`（`/api/v2/modernization`）

- 目标登记默认 `candidate`；只有 `confirm_dimension` 能把某个维度提升为
  `active`/`decision`，且走同 key 的 CAS 更新，知识库因此记得下是谁确认的。
- 一条数据库维度的切片真的过 `execute_plan`：基线先真失败（缺 dm 方言），
  修复后 `git format-patch` 导出的补丁应用到全新 clone 能让检查真正转绿。
- 没有达梦实例就把数据库验证记为未验证，不写成通过。

**自动化三方接口适配** `api_adaptation.py` + `adaptation_routes.py`（`/api/v2/adaptation`）

- 本地模拟第三方端点走真实 TCP；`mock` 在契约导入时声明并原样带进回执，不是事后贴标签。
- `technical_success` / `business_accepted` / `business_completed` 三个字段分开，
  200 与 mock 通过都不推出业务完成（本轮单向上报，`business_completed` 为 False）。
- 无法映射的字段（`metadata`）标为待确认并写明影响，同时落进项目记忆，不静默丢弃。

**自动化运维** `issue_maintenance.py`

- 出回执时把这次改到的路径与 commit 写回项目记忆，状态 `candidate`；
  同一 `(task_id, revision)` 重复导出不会写第二条。
- 视图字段定名 `project_memory` 而不是 `memory_refs`：它是实时读的「项目现在知道什么」，
  不是这条任务派发时刻真正带走的那一组。回执刻意不带这个字段——
  冻结的交付记录不能引用一个还会变的值。**「派发时刻的冻结引用」没有做，见未验证项。**

**集成侧的四处改动**

1. 两个新场景声明为 `executable` 并接进 `app.py`，但 `default_enabled=False`：
   新装的场景默认关着，由管理员按客户打开。升级不会替所有存量客户静默开两个业务面。
2. `plugin_routes` 为两个新插件各接自己的活跃执行判定，没有给不知道的插件填假的 0。
3. 补了信创 `approve` 的闸门测试——这条路绕过端口，端口自己的测试覆盖不到它。
4. 「没有处理器就不能启用」这条规则现在没有任何已发布插件处于那个状态，
   改为临时把一个声明降级来保持覆盖，而不是把测试删掉。

**必要检查**：相关后端 190 passed；前端 22 passed、生产构建通过。
新增 4 个变异全部被杀死（信创 approve 不问闸门、适配交出未包装端口、
新插件默认自启、交付事实被写成已确认）。

## 模型事实

- 集成者（本会话）：请求的是 Claude Code 默认会话模型，实际为 **Opus 5（`claude-opus-5`）**，
  取自会话 system prompt 的模型元数据。**用户要求的 Sonnet 5 没有用在集成者这一侧**——
  会话模型不能由我自己中途切换，这里如实记录，没有伪报。
- 三个场景执行者：以 `model: sonnet` 派发，三个子会话都逐字回报了同一行
  system prompt 元数据：`You are powered by the model named Sonnet 5. The exact
  model ID is claude-sonnet-5.` 即三个场景的编码实际由 **Sonnet 5
  （`claude-sonnet-5`）** 完成，与请求一致。
- 没有打断任何正在工作的会话去强切模型。

## 未验证项

- 真实模型 coding 质量、客户业务效果、生产部署：不在本轮范围，也没有执行。
- 独立安装/独立发行包：**没有执行过**，因此不写「可独立部署已验收」。
- 插件停用与启用没有在长时间真实并发下验证；排空判定与状态写入之间的竞态窗口
  已由 `approve` 的闸门兜住，但窗口本身没有被消除。
- 「派发时刻的冻结记忆引用」没有做：`project_memory` 是实时读，不是这条任务
  派发那一刻真正带走的那一组。要做需要在 `WebuddyExecution.submit` 里把 recall
  到的条目随 run 一起持久化。当前命名与回执的取舍已经把话说清楚，没有假装是绑定。
- 三场景的执行链都用注入的 dispatch 跑过（与既有维护垂直测试同一手法），
  **真实 LLM 规划 + 人工 approve 的完整 dag 路径本轮没有跑**。
- 信创只验证了 `database` 一个维度的完整闭环；CA/签章、OS/CPU、浏览器、
  外部组件四个维度的命中词表没有在真实项目上验证过。
- 适配只跑了 `mock` 环境；`sandbox`/`production` 分支代码可用但没有被真实调用验证过。
- 没有任何一条记忆条目被提升为 `active`（除测试内的合成数据），
  因为没有真实客户确认人。
- 前端只做了插件启停页与维护页的可用性联动；信创与适配**没有页面**，
  本轮只有 HTTP 面。
