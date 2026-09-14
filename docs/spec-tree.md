# 规格树与机械漂移检查（L0）

管理员在项目设置启用“规格树”后，项目页出现同名 Tab。默认关闭，原项目执行、验收与费用行为不变。“生成规格树”创建普通 webuddy 运行，沿用现有队列、并发上限、项目预算、审批政策、独立验收与平台提交权。新节点 raw source 留空，等待人确认；这不是额外的模型调用通道。

## 数据格式与兼容性

以 [SpexCode](https://github.com/shuxueshuxue/Spexcode) 的 `d4f370b2909c9842a01ecd30c8f3a8772a9aba7b` 为兼容基线，核对其 `spec-cli/src/init.ts`、`packages/spec-core/src/specs.ts` 与纯初始化模板。根模板保留上游格式，[MIT 许可证](licenses/SpexCode-MIT.txt) 随附。

数据仍是原版 spex CLI 可接管的 `.spec/` 资产，不需要转换，也不需要 webuddy 专属 frontmatter 字段。解析器遵循上游的**逐行标量／短横线列表语法**，不是完整 YAML；因此不要把 `code:` 写成 YAML 内联数组，也不要用 YAML 块标量。额外上游字段（例如 `hue`）保留，不进入运行权限。

```text
.spec/
  spexcode.json
  项目名/
    spec.md
    子领域/
      spec.md
```

```markdown
---
title: 订单金额
status: active
desc: 订单金额换算与精度边界
code:
  - src/orders.py#calculate
related:
  - README.md
---
## raw source
金额精度必须保留。

## expanded spec
以分为内部计算单位，导出时转换为元。
```

每个含 `spec.md` 的目录是一节点，父节点为最近的含规格文件的祖先目录。`code` 管辖代码，`related` 仅提供关联资料，不触发 drift。正文分段遵循上游的 `## raw source` / `## expanded spec`，忽略代码围栏中的同名标题；没有这些分段的存量正文作为 expanded 展示。非法 frontmatter 节点返回 `invalid` 和原因，其他节点仍可读取。路径穿越不被接受，规格目录／文件符号链接不会被跟随。

首次启用时，生成等价 `spex init --pure --title <项目名>` 的数据文件：纯根模板与 `{"dashboard":{"title":"项目名"}}` 配置。标题按上游规则规范为目录 ID，非法名称回退到 `project`。已有规格树不覆盖、不强制改版。为满足 webuddy 的干净基线要求，平台随后通过独立索引与 Git plumbing 将新骨架归档为一个提交；不运行任何 hooks，不混入用户修改。需要干净工作区并检出项目基线分支。归档不会自动推送或部署项目。

项目开关保存在现有项目 JSON，使用项目 revision CAS 和 `project_settings_audit`；关闭不会删除 `.spec`。规格版本与 drift 不写数据库状态表或缓存文件：版本史就是触及该 `spec.md` 的提交列表。

## Drift 判定与边界

1. 固定被检查的 commit，从 Git blob 读取该提交的规格树，以每个节点最后一次规格变更提交为基准。
2. 基准之后触及 `code` 路径的提交产生 `file`；支持精确文件、目录前缀及 `*` 路径匹配。规格与代码同一个提交更新时不会产生 drift。
3. 对 `path#symbol`，分别读取变更前后的符号范围，并与零上下文 diff 的新旧 hunk 行区间求交。命中升级为 `anchored`，删除符号使用旧范围仍可命中。
4. 符号范围只用声明启发式：定位 `def/class/function/const symbol`，直到下一个同级或更外层声明。不安装语言解析器。类方法限定名、重载／重复同名、动态定义等不能可靠定位时，记录 `symbol_unresolved` 并保留文件级提醒。范围可能包含注释或相邻非声明代码；这是保守的变更信号，不是语义正确性的证明。
5. 合并提交逐父提交比较；路径改名视为删除／新增，不追踪历史改名。浅克隆不能证明历史完整，返回未验证。未提交的工作目录修改不用于验收；验收只针对固定 SHA。
6. 单次 drift 调用内复用 Git 读取结果，45 秒总时限，每条 Git 命令最多 20 秒。单节点读取最多 256 KB，最多 2000 节点；超限、缺失历史或 Git 错误均降级为显式 unverified，不能伪装“无漂移”。不持久化缓存或状态文件。

启用项目的独立验收 ledger 增加 `spec-drift:<节点路径>` 机械项：anchored → fail；file → unverified；none → pass；invalid → unverified。原始需求证据先完成现有覆盖检查，再合并机械证据，避免把机械漂移错误当成模型漏报而无谓重试。无 `.spec` 或未启用不产生证据项。Git 故障只让该项 unverified，其余验收继续留证据。

旧式非托管 DAG 原本不调用独立模型验收时，仅追加 `scope=mechanical-spec-only` 的 Git ledger，不为 drift 增加模型费用；规格树生成任务则显式走完整独立验收链。

独立验收副本没有 `.git`；平台从源仓库按 `verification_commit` 计算，模型仍只接触隔离副本。巡检复用同一验收流程，所以相同机械项保留在巡检 artifacts / ledger；没有额外执行阶段或模型费用。

执行与验收通过现有 `module_prompt` 挂载规格规则。规划完成后，按任务 focus/paths 找到管辖规格，向每任务提示词追加最多 2000 字符正文摘录，截断时说明；DAG 的相应 spec 路径同时纳入任务范围和调度冲突检查，允许代码与规格由平台一起提交。raw source 是人签意图，Agent 收到原样保留的约束；如需变更，须在交付说明中提出，本轮不提供人签编辑器或新的授权流程。

## 工作台与 API

- 树为缩进列表，不做图谱；每行展示标题、status、漂移徽标、管辖文件数，顶部汇总节点和漂移数量。
- 节点详情依次展示人签意图、Markdown 展开规格、管辖文件与符号、related、漂移提交、规格版本史。Markdown 使用共享组件，禁用原始 HTML，标题限制在卡片内容层级。
- 验收 ledger 的规格项可直接打开对应项目节点。
- `GET /api/v2/projects/{pid}/spec-tree`：树摘要与 drift。
- `GET /api/v2/projects/{pid}/spec-tree/node?path=.spec/.../spec.md`：完整详情。
- `PUT /api/v2/projects/{pid}/spec-tree/settings`：管理员开关，body 为 `{enabled, revision}`。
- `POST /api/v2/projects/{pid}/spec-tree/generate`：管理员创建普通初稿运行，body 为 `{idempotency_key}`；同键重试只入队一次。

读取与写入都校验现有项目授权；写接口仍受管理员与 CSRF 保护。UI 默认读取项目 HEAD 的规格；从验收证据跳转时携带运行 ID，读取该运行验收 SHA 的规格快照，避免新节点尚未合并时跳到错误版本。两条 GET 接口均支持可选 `run_id`，仍校验运行属于当前授权项目。未提交草稿不作为已归档节点展示。API 文本经过现有脱敏管道。规格内容不授予工具权限。

## 与上游的差异和明确不做

仅复刻 L0 数据格式和机械检查，自建列表及详情。没有 SpexCode 的 L1 tmux/session 管理、L2 dashboard/图谱、atlas 插件、Git hooks、pre-commit 守卫、`--harness` 契约物化；不写 CLAUDE.md / AGENTS.md，不依赖 spex CLI 运行时，也不解析 `[[节点]]` 需求引用。上游更精细的语言解析、持久化索引和会话归因不在本轮范围。

原版 CLI 接管资产不意味着两套 drift 引擎对所有语言都会得出相同级别：本轮故意采用上述简单、可解释的保守范围实现。

## 验证

`tests/test_spec_tree.py` 覆盖格式容错、嵌套、锚点、同提交、文件与符号变更、删除符号、符号降级、无规格、Git 失败、任务摘录、初始化无 hooks、revision 冲突、项目成员授权、生成去重、真实验收副本接入和巡检证据。前端 `SpecTree.test.tsx` 覆盖列表、详情、状态、Markdown 安全、验收跳转和配置／生成请求。

浏览器隔离样例的规格树截图与 axe 报告见 `docs/design/spec-tree/`；样例不触发真实任务或部署。axe 的 incomplete 保留，零检测违规不等同完整人工无障碍认证。

兼容性补充核对：使用该上游提交中的原始 `parseFrontmatter` 函数，验证纯根模板与本文的 `path#symbol` 列表均可直接解析。

## 执行范围声明与对账

启用规格树时，worker 必须在第一次修改文件前发送独立的 assistant JSON 消息：

```json
{"scope_declaration":{"files":[{"path":"src/app.py","spec_nodes":[".spec/project/spec.md"]}]}}
```

`path` 为仓库相对的精确文件路径，不接受目录、通配符或 `#symbol`；没有管辖节点时 `spec_nodes` 可为空或省略。后续新增文件须先发送同格式追加声明；空数组不撤销已有声明。平台将其转换为只追加的 `scope_declaration` 事件，记录事件时间、任务 id、基准提交、文件、节点及是否为迟报。事件不可更新或删除，归档后仍可读取完整内容。只识别 assistant 消息，不采信工具输出中的 JSON。

接收声明时，平台将工作区（包括未提交和未跟踪文件）与运行规划基准比较：此前未声明且已经发生改动的文件标为迟报，不加入有效声明集合。正常已声明文件重复出现不失效。最终独立验收在同一基准与验收提交之间进行只读 Git diff，增、删、重命名两端都按路径核对；不使用模型判断或增加模型调用。`scope:reconciliation` 证据记录 `undeclared_changes`、实际文件、有效声明、豁免文件及声明事件 id。未声明改动非空为 fail；Git 失败或缺少基准为 unverified，均保留原因，不使验收进程崩溃。旧 DAG 的机械验收入口执行相同检查。

明确豁免如下（区分大小写）：

- `.spec/` 下的规格树文件。
- 文件名符合 `test_*.py`、`*_test.py`、`*.test.js/jsx/ts/tsx`、`*.spec.js/jsx/ts/tsx` 或 `*_test.go` 的测试文件。

不按 `tests/`、`test/` 目录整体豁免；`conftest.py`、测试配置、夹具、构建配置和 `.webuddy/` 下的文件仍须声明。豁免只免范围对账，不免其他验收。未启用规格树时不收集声明也不产生此证据；只读巡检没有 worker 修改过程，保留既有 drift 证据，不对历史代码追补本轮声明。

节点详情显示最近 20 条涉及其 `code:` 管辖文件的声明，附运行短号、时间、任务与运行对账状态。未完成验收显示“待对账”，不提前显示通过；关联依据节点的实际管辖文件，不能靠 worker 虚报节点建立关联。越界徽标代表该运行整体对账结论。

这是声明与最终 Git 事实的对账，不是系统调用隔离：不能证明某次未提交改动在声明前已经发生又被完全撤销，也不能观察被忽略且未进入最终提交的临时文件。没有引入 Birdview 代码、architecture.json、git 钩子或新的远程权限。
