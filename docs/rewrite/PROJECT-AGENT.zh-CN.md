# Project Agent：工程记忆、代码图与交付边界

本文件是当前重写增量的实践说明，配套接口契约见
[PROJECT-AGENT-CONTRACT.md](PROJECT-AGENT-CONTRACT.md)。它描述已经落到代码路径的边界和使用方式，不能替代
[STATUS.md](STATUS.md) 的阶段判断；根代理集成回归完成前，不把本增量称为完整 P2 或生产认证。

## 1. 运行前提与模块位置

Project Agent 是同一控制面的工程辅助能力，面向单个可信工程师工作站。登录用于访问控制；它不是多租户 ACL，也不是
执行 Agent 的 OS 沙箱。现有可信检查、计划审批、执行策略和 GitHub 凭据边界继续有效。

| 能力 | 代码路径 | 主要用途 |
| --- | --- | --- |
| 工程配置、路由和上下文接线 | `factory/control/project_routes.py`、`context.py`、`service.py` | 工程、运行、快照和冻结上下文的生命周期 |
| 工程档案和知识 | `factory/control/knowledge.py` | 版本化档案、候选/活动/退休记忆、审计和显式导入 |
| 代码图 | `factory/control/codegraph.py` | 从固定 Git 对象建立有限、可追溯的定位证据 |
| GitHub 交付 | `factory/control/github.py` | 发布后独立核验 PR 是否真正合并 |
| 工程工作台 | `frontend/src/workspace/ProjectAgent.tsx`、`RunKnowledge.tsx` | 查看档案、知识、代码搜索/图和运行时冻结证据 |
| 针对性验证 | `tests/test_project_knowledge.py`、`tests/test_project_codegraph.py`、`tests/test_control_github.py` | 本地行为验证；最终 root regression 仍待完成 |

## 2. 代码图：固定提交的定位证据

代码索引不是运行代码，也不证明运行时行为。

1. `baseline_sha(project)` 只解析已登记仓库的
   `refs/heads/{base_branch}`，分支名先过 Git 格式校验；Git 命令使用参数数组、超时和非 shell 调用。
2. `build_snapshot(project)` 用该完整提交 SHA 的 `git ls-tree`、`git cat-file` 读取 tracked blobs，不读工作区文件，
   不 checkout，不调用模型，也不执行项目脚本。未提交的脏文件不会进入图。
3. 索引拒绝 symlink、submodule、`.env*`、密钥/证书/凭据、依赖与生成目录，以及 `.claude`、`.codex`、hooks 和配置敏感路径。
   只接受非敏感的 Markdown、Python、JavaScript/TypeScript/TSX 和 JSON。
4. 路径数、单文件字节数、总 source bytes、节点数和边数都有上限；超过上限会在 `warnings` 和 `stats` 中保留跳过/截断计数，
   Git tree 输出过大则明确失败，避免把巨大仓库一次性读入内存。
5. Python 用 `ast.parse` 识别文件、类、函数、contains/imports/calls；声明、contains/imports 可标 `syntax`，但所有静态推断的
   calls 都标 `resolution=heuristic`（属性调用、关键字参数/导入遮蔽和无法确认作用域的调用会省略）。语法错误只是 warning。JS/TS 使用保守正则并标为
   `resolution=heuristic`。无法解析到真实节点的调用不写成事实；所有边的两端都必须是图中节点。
6. 节点 ID、边排序和搜索评分确定性可重建。只保存脱敏的有界 symbol snippet（公开 Markdown 也只保留有界搜索片段），不保存完整源码。
   这是一种 locator evidence，不是运行时证明。

典型后端用法：

```text
POST /api/v2/projects/{pid}/code-index       # 建立固定基线快照，只返回元数据
GET  /api/v2/projects/{pid}/code-index       # 查看 indexed/current SHA 与 stale
GET  /api/v2/projects/{pid}/code-search?q=  # 有界的名称/路径/文档搜索
GET  /api/v2/projects/{pid}/code-graph?node= # 文件或符号的一跳图切片
```

`save_snapshot(store, snapshot)` 使用同一 SQLite Store 的追加表，按
`(project_id, commit_sha, parser_version)` 去重；提交或 parser version 变化会产生新快照。

## 3. 工程记忆：已审核内容与候选内容分开

“记忆”不是一个可以任意覆盖的提示词文件，而是有版本和来源的工程记录。

- 工程档案（agent profile）保存名称、使命、架构摘要、约束。更新使用 revision CAS；旧版本保留。
- 知识条目有 `fact`、`decision`、`hypothesis` 三种 kind，和 `candidate`、`active`、`retired` 三种 status。
- 规划上下文默认只取当前、未过期的 `active` 条目；候选条目展示给人审核，但不能悄悄升级成事实。
- 人工创建/批准保留 provenance 和 reviewer 信息。GitHub 合并事实必须来自独立核验的运行与提交证据，不能把计划叙述或模型自报完成写成事实。
- 每次版本和审核动作追加 audit；同一条目更新必须带上一个准确的 expected revision。跨工程 ID、任意 provenance 或直接改历史版本都会被拒绝。

使用路径：先在 Project Agent 的“档案/知识”页编辑或审核；需要 API 时使用
`GET/PUT /projects/{pid}/agent`、`GET/POST/PUT /projects/{pid}/knowledge` 和
`GET /projects/{pid}/knowledge/{key}/versions`。看到 `candidate` 时，应先核对来源、路径和提交，再决定是否批准；批准不改变其原始导入来源。

## 4. TeamAI 文档导入：明确数据交换，不执行资源

导入入口接受用户明确提交的 JSON bundle：

```json
{
  "repository": "owner/name",
  "documents": [
    {"path": "teamwiki/release.md", "content": "..."}
  ]
}
```

流程是 `POST /api/v2/projects/{pid}/wiki-import/preview`，人工查看服务器生成的文档摘要、hash、warnings 后，再以
`POST /api/v2/projects/{pid}/wiki-import/apply` 选择 index。预览不可变、按项目和 hash 绑定、重复应用幂等；导入条目始终是
`source=teamai_import` 的 hypothesis/candidate，绝不是已验证事实。服务校验仓库匹配、路径安全、Markdown 类型、文档数量和字符预算，
并在落库前脱敏。

单文档最多 16000 字符，超过 8000 字符会分段生成可审批条目。来源保留 `original_sha256`（原始输入的哈希）、
`redacted_sha256`（脱敏全文的哈希）、`chunk_index` 和 `chunk_count`；原始秘密文本不会落库。

这里明确不运行 TeamAI 原生 CLI，不加载它的 resource injection，不执行 wiki 中的 hooks、MCP、YAML、脚本或任意 URL。文档内容只作为
不可信数据交给审核和上下文组装；导入/索引阶段没有模型调用。

## 5. 运行上下文是有界且冻结的证据

规划前，控制层把以下内容组装为有界上下文：工程档案、当前活动且未过期的知识、与同一 baseline SHA 对齐的代码搜索结果，以及明确的 warnings。
候选知识、stale 快照和不同提交的命中不会混入为活动证据。上下文总长度有上限，超出时保留确定性截断/警告。

代码索引本身可以在脏工作区中运行且仍只看提交对象；但规划前的 checkout 校验要求 `HEAD == baseline_sha` 且 `git status` clean，规划前后都要复核。上下文写入运行记录和 `context.assembled` 事件，总计最多 12000 字符；每条知识正文最多载入 1000 字符、每个代码片段最多 800 字符，并保留版本、来源与截断标记。运行查看接口返回这份冻结副本。后来有人更新知识或重建代码图，不会改写该计划版本看到的内容；主动补充需求重新规划会产生新快照，旧快照保留在事件记录。
审批时再次核对 baseline SHA；分支漂移就拒绝继续，要求工程师核对 checkout 并重新规划，建议同时更新索引。提示词把检索内容标成不可信证据，不能改变检查、模型、权限或任务范围。
若上下文需要历史辅助信息，只允许未编辑的原始 `github_merge` fact，且其合并提交必须是当前 SHA 的祖先；记录为 `applicability=historical_merge`，不能据此证明当前代码行为或当前检查通过。人工改写过的描述不能借用这条例外。

## 6. 合并确认是独立核验，不是自动合并

发布动作只推送已验证分支并创建或复用 PR，现有运行状态仍是 published/review 等状态，不因为出现 PR 就称为 merged。
人工按钮或签名的 `pull_request.closed` webhook 触发 `GitHubDelivery.observe_merge(project, run)`：控制面重新 GET 精确仓库的 PR，核对
base repo/ref、head repo/ref/SHA、PR URL、merged、merged_at 和 merge commit SHA。查询失败、仓库/分支/提交变化、仅 closed 未 merged 或 malformed SHA 都不会生成事实。

通过核验后才记录一条简洁、幂等的 active fact，内容仅描述本次 run/commit/check 结果已合并，保留 run、PR、head、merge SHA、base branch 和时间证据；不把计划叙述升级成事实。
合并核验流程不 push、merge、close、comment，也不自动合并 main。本地基线不会自动拉取远端；新合并记录在本地历史中不可验证时会被排除。工程师更新本地基线后，旧索引显示 stale，需重新建立索引。

## 7. 运维和当前限制

- 这是单实例、单所有者/可信工程师工作站；同一实例账号共享工程空间，不宣传项目 ACL 或租户隔离。
- Trusted Host、HTTPS、登录和 CSRF 仍是入口边界。公网不要暴露旧 `factory api`。
- worktree 只隔离文件变更，不等于 OS/容器沙箱；控制面和 provider worker 当前不构成强秘密隔离。
- 三家 SDK 的真实账号、权限、成本、超时取消和隔离认证不由本地替身测试代替；付费 SDK E2E 仍待环境配置。
- 完整大日志、持久租约/恢复、worker 隔离、团队 ACL 和后续自动合并策略属于后续阶段。本地回归结果与未验证范围见 `STATUS.md`；通过本地测试不等于生产上线。
