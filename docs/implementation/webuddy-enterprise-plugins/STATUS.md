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

## 模型事实

- 集成者（本会话）：请求的是 Claude Code 默认会话模型，实际为 **Opus 5（`claude-opus-5`）**，
  取自会话 system prompt 的模型元数据。**用户要求的 Sonnet 5 没有用在集成者这一侧**——
  会话模型不能由我自己中途切换，这里如实记录，没有伪报。
- 三个场景执行者：以 `model: sonnet` 派发的子代理，各自回报自身 system prompt 里的
  精确模型 ID（见下面的场景批次）。

## 未验证项

- 真实模型 coding 质量、客户业务效果、生产部署：不在本轮范围，也没有执行。
- 独立安装/独立发行包：**没有执行过**，因此不写「可独立部署已验收」。
- 插件停用与启用没有在长时间真实并发下验证；排空判定与状态写入之间的竞态窗口
  已由 `approve` 的闸门兜住，但窗口本身没有被消除。
- `legacy-modernization` 与 `api-adaptation` 仍是 `executable=False`，
  按契约不挂假就绪入口；要翻成 True 需要先核对处理器真的可用。
