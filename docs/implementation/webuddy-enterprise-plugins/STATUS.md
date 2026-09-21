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

## 批次五：语言范围与初审四点（提交 `c60ab76`、`c504a4d`、`74fcd31`、`f9f9ab8`）

### 1. 图工具真的接了（初审第 1 条）

工具：**`@colbymchenry/codegraph` 1.6.0**，MIT，tree-sitter 语法 + Rust 内核，
本地 SQLite 索引，CLI；`npm i -g` 装的（没用 `curl | sh`）。首次运行会写
`~/.codegraph/telemetry-queue.jsonl`，已执行 `codegraph telemetry off`，
缓冲里那一条（只有一个 `version` 命令计数，不含代码）已被删除。
**没有**运行 `codegraph install`——那会改写全局 agent 配置。

仓库内的 `codegraph.py` 保留为回退，没有改名冒充。它的空结果现在带 `covers`
字段说明它只解析 Python 与 JS/TS 且只读已提交的 commit。

### 2. 四种必需语言，分三层记录（初审第 3 条 / LANGUAGE-SUPPORT.md）

| 语言 | 代码检索 | 跨文件关系 | 构建与改造 |
|---|---|---|---|
| Java | 已验证 | 已验证 | 按项目探测（本机有 javac 19，无 maven/gradle）|
| Python | 已验证 | 已验证 | 按项目探测（本机 3.12.7）|
| C#/.NET | 已验证 | 已验证 | **未验证**：本机没有 dotnet |
| Go | 已验证 | 已验证 | 按项目探测（本机 go1.26.3）|
| C/C++（可选）| 已验证 | 已验证（仅最小样例）| 未验证 |
| Rust（可选）| 已验证 | **未解析** | 未验证 |

「已验证」= `tests/test_code_intel.py` 用**真实后端**在最小两文件跨文件样例上
断言过，每种必需语言各一个代表性跨文件查询 + 一次改动后的刷新验证。
**这不等于在客户仓库上验证过。** 没有做跑分矩阵、没有变异测试。

**C++/Rust 是否后置**：C++ 现成能力可用，已接入并如实标为「最小样例已验证、
宏/条件编译/模板未验证」。**Rust 后置**——符号能索引能检索，但同样形状的
跨文件调用边没有解析出来（callers 与 callees 都是空），所以关系层对 Rust
**拒绝**而不是返回空列表；空列表读起来就是「没人调用」。

**C# 的 .NET 区分**：索引读的是 `.cs` 语法，它不知道目标框架。
`.NET Framework` 与现代 `.NET` 由 `.csproj` 的 `TargetFrameworkVersion` /
`TargetFramework` 判定，读不到就是 `unknown`，不猜；前者还要求 Windows。
「.cs 能解析」不等于 WebForms/WCF/P-Invoke/私有 DLL 已支持。

### 3. 三件必须由适配层纠正的后端行为（都是实测出来的）

1. **`callers`/`callees` 按符号名匹配，跨语言串味。** 混合仓库里一次
   `callers format_money` 同时返回了 C++ 和 Python 的调用方；Rust 的查询
   返回了 C++ 的调用方。本层按定义所在语言过滤，并把丢掉的部分报出来。
2. **空结果路径不认 `--json`。** 「找不到符号」「项目没初始化」都是人话句子，
   后者退出码 1。分别映射成 `no_match` 与 `not_indexed`。
3. **截断无声。** `--limit 2` 对三个调用方只返回两个且不提示。本层多取一个
   自己判断，报 `truncated`。

结果契约：`outcome` ∈ ok / no_match / not_indexed / truncated / unsupported /
backend_unavailable / degraded_text，每条都带 `code_version`（commit + 是否有
未提交改动）、`freshness`、`unresolved`（反射、动态导入、P/Invoke、私有 DLL、
接口动态分派、cgo、跨语言 RPC/FFI/数据库/配置等看不见的关系）。
关系后端不可用时回退文本检索并标 `degraded_text`，**不标成关系分析**。

入口：`POST/GET /api/v2/projects/{pid}/code-index`（一个动作建两套索引）、
`GET /api/v2/projects/{pid}/code-conditions`、`GET /api/v2/code-intel/capabilities`。

### 4. 记忆边界（初审第 2 条）

按 `kind`+`status` 分出五种角色，不新增列：`constraint`（decision+active，
唯一有约束力）、`fact`、`decision`、`observation`、`lead`。未确认的资料现在
带正文出现在「不作为依据」标题下——先前只给标题，等于把悬而未决的问题
永久变成不可读。声明了 `paths` 的条目只在本次改动碰得着时才装载；
装不下的按角色优先级丢（约束最后丢），丢掉的条数在提示词里说出来。

**派发时冻结**：`submit` 把真正装载的 `(key, revision)` 写进 `run.source`。
`knowledge_entry_versions` 追加写，所以这一对永远指向同一份不可变内容。
任务视图优先读它并标 `frozen=true`；读不到时退回实时读并标 `frozen=false`
加原因，不让实时读冒充「本任务的依据」。执行开始后项目再确认新要求不会
改变这条任务的依据。

### 5. 业务入口（初审第 3 条）

`/modernization` 与 `/adaptation` 两个页面已接入 AppShell 与既有任务现场：
可发起、看进度与阻塞、批准/继续/取消、取回执与补丁、以修订形式继续反馈。
适配页把技术成功／业务受理／业务完成三个状态分开呈现，mock 单独打标。

集成时修掉一处我自己造成的契约错配：`locate` 的 `source` 值在图后端接入时
改了，而页面仍按旧值判断，会把一次成功的共享层定位显示成「未建立代码索引」。

### 6. 变异测试已停（初审第 4 条）

本批次没有做任何变异测试。

**本批次检查**：`tests/test_code_intel.py` 19 passed（真实后端驱动，
未安装则跳过并说明原因）、相关后端 178 passed、七个场景页面 83 passed、
tsc 干净、生产构建跑了一次。

## 批次六：初审 cb74bab 的四点修正与两条真实页面闭环（`fc7b5fd`、`dcced0f`）

### 代码图三处确定性问题

1. **后端失败不再读成空结果。** `_run` 返回结构化调用结果，`_classify` 把
   超时、非零退出、JSON 解析失败、退出 0 但输出无法识别分别归类；只有后端
   自己那句已确认的 `Symbol "x" not found` 才允许变成 `no_match`。
   每个结果带 `answered`。
2. **过滤后不再谎称完整。** 有界超取（`limit*5`，上限 200），一页取满就报
   `incomplete`，既不宣称无匹配也不宣称未截断；不做无限拉取。
   另外 `callers` 按名字查不锁定被调用方身份：同语言同名多个定义返回
   `ambiguous` 与候选，要 `target_path` 指定；答案带 `target` 说明解析到了哪个。
   `import` 节点不算竞争定义——否则每次 Python 查询都和自己歧义。
3. **索引隔离。** 索引改到 `index_home()` 下独立的 git worktree，按 commit
   钉住并维护 (源码树, 已索引 commit) 映射；执行者会改的那棵树里根本没有
   `.codegraph`，不再靠 `.git/info/exclude` 假装隔离。代价如实记录并有断言：
   未提交的改动不在索引里，所以每个答案都带 `indexed_commit` 与
   `source_commit`/`source_dirty`。实测后端在 worktree checkout 之后
   `pendingChanges` 仍为 0，所以刷新信号取我们自己的映射，commit 一变就整建重建。

### 记忆两处

必遵要求要么完整带上、要么阻塞，上限就是预算本身（旧代码让第一条无论多大都
放行，7000 字的要求整条带入 6000 预算，再把后一条必遵要求挤掉）。摘要只用于
背景资料，绝不用于要求——截短的必遵要求是没人能遵守的要求。
维护装配显式 `scope_paths=None` 并记进 `memory_scope_known=False`：
此前没传就等于没做按路径筛选却像做了。计划批准后 `refine_memory_for_plan`
按计划声明的文件重新取范围，两份依据都留着。

### 方法版本

`/api/v2/adaptation/agreement` 读实际安装的方法包，创建与修订都用它覆盖请求里
的值；页面改成只读展示。实测：请求里塞 `v999-伪造` / `forged@42`，落库与回执
都是 `v1` / `api-adaptation@1`。

### 两条真实页面闭环（`scripts/preview_scenarios.py`）

起了真实服务（127.0.0.1:8791），用真实 HTTP 走完两条链，并在浏览器里看过页面。

**信创化改造**：停用→登记目标被拒 409 →启用→登记目标（默认 candidate）→
人工确认「数据库/版本」一维（其余不受影响）→处置计划→建切片→计划就绪
（阻塞原因「等待人工批准」）→批准→**项目自己的 pytest 真的失败了一次**
（`check.result exit=1`、`attempt.failed`）→修复后 `regression` 通过→已交付→
回执→下载 583 字节真实补丁（`db_url.py` 增加 dm 方言）。
回执未验证项同时包含调用方声明的「没有达梦 DM8 真实实例」和自动判定的
「本轮检查未覆盖数据库维度」。

**接口适配**：服务端方法版本→建任务（伪造版本被覆盖）→计划→批准→已交付→
`contract` 检查真的起了本地模拟端并发了一次 POST →回执三状态分开
（technical=True、accepted=True、**completed=False**）、mock=True、
脱敏结果只剩 `{code, request_id}`→737 字节真实补丁（`adapter.py` 补鉴权头）。

**这条链上真正发现的洞**：适配面**没有 approve 入口**，运行停在
`awaiting_approval` 后端口自己的方法都不碰批准，从页面建出来的适配任务永远动
不了。单元测试看不见它——那些测试注入了 dispatch，跳过了批准。已补齐并加了
页面上的待批准计划与批准按钮。

### 模型条件（如实）

**没有真实编码模型条件。** `claude_agent_sdk` / `openai_codex` /
`deepseek_harness` 三个 provider SDK 在本机**都没有安装**，`ANTHROPIC_API_KEY`
与 `OPENAI_API_KEY` 都未设置；平台的 SDK worker 还刻意剥掉宿主 Claude Code 的
凭据（`_HOST_CLAUDE_CODE_ENV_KEYS`），即宿主 agent 的凭据按设计不借给工厂。
所以上面两条闭环里**编码这一步是带标注的脚本**，不是模型。
真实的是：服务、HTTP 契约、控制库、插件闸门、项目自己的检查、git 与补丁。
要跑真实模型链，需要装其中一个 provider SDK 并配置该 provider 自己的凭据与预算。

**本批次检查**：相关后端 180 passed（含每处的定向复现）、场景页面 74 passed、
tsc 干净、生产构建一次、两条真实服务闭环如上。按初审要求未做变异测试、未重复全量。

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
- 两条页面闭环已在真实服务上跑通并在浏览器里看过（见批次六），但**编码这一步
  是脚本不是模型**；真实模型条件缺失，原因与补齐方式见上。
- 代码图的语言能力仍只在最小样例上验证；本轮没有扩展可选语言。
- C#/.NET 的构建与改造层在本机无法验证：没有 dotnet，.NET Framework 还需要
  Windows。索引与关系层与框架无关，已验证；改造层标待验证。
- 四种语言的关系层都只在最小两文件样例上验证过，没有在任何客户仓库上验证。
- `.codegraph/` 自带 `.gitignore`（`*`），内容对 git 天然不可见；本层另把目录名
  写进 `.git/info/exclude` 以免污染 worker 的 diff。代价是放在该目录下的代码
  在任何 git 形状的评审里都看不见，而跑在真实树上的检查仍能执行它。这条**没有解决**。
- 适配模块没有 `/api/v2/adaptation/agreement` 端点，所以页面上的方法版本是
  人工输入，不像维护页那样自动取方法包信息。
