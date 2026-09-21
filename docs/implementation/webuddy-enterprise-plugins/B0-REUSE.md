# B0 复用清单：项目记忆与代码关系查询（集成者裁定，三场景共用）

2026-09-21｜基线 `741a55b` 之后｜**这一层已经存在，不要再建第二套。**

## 项目记忆 = `factory/control/knowledge.py::KnowledgeStore`

SHARED.md 要求项目记忆带「来源、确认状态、适用范围、时间与代码版本」。已有表
`knowledge_entry_versions` 逐项对上：`provenance`（来源，服务端生成，请求不能伪造）、
`status`（`candidate` 未确认 / `active` 已确认 / `retired`）、`paths`（适用范围）、
`created_at`（时间）、`commit_sha`（代码版本）、`kind`（`fact`/`decision`/`hypothesis`）。
版本表追加写、有触发器挡改删，另有 `knowledge_audit`。

**统一入口是 `factory/control/scenario_memory.py`**（集成者所有，不要改）：

```python
from factory.control import scenario_memory as sm
sm.record(store, project_id, plugin_id='issue-maintenance', topic='数据库兼容',
          content='...', actor=username, status='candidate',
          paths=['db/schema.sql'], commit_sha=sha)   # 默认未确认
sm.recall(store, project_id)                          # 默认跨场景，退休条目已排除
sm.recall(store, project_id, plugin_id='api-adaptation', confirmed_only=True)
sm.constraints_block(entries)                         # 给执行器提示词用的那一段
```

三条硬约束：

1. **key 由服务端生成，选不了。** 场景归属靠标题前缀 `[信创]/[运维]/[适配]`，
   由 `sm.title_for` 生成。这是分组约定，不是授权边界，不要当成隔离。
2. **`candidate` 不是 `active`。** `constraints_block` 只把已确认条目的正文放进
   提示词；未确认的只出标题，且在「不作为依据」标题下。报告里的建议是
   `candidate`，客户批准过的才是 `active`。这条已被变异测试盯住，别绕开。
3. **更新用 CAS**：`put_entry(..., key=..., expected_revision=...)`，
   `provenance` 不允许由请求替换。

维护任务的执行器提示词已经带上这一段（`issue_maintenance_webuddy.submit`）。
信创与适配接自己的执行入口时按同样方式取，不要各写一份取数逻辑。

## 代码关系查询 = `factory/control/code_intel.py`（共享层）

> **2026-09-21 更正。** 本文档先前写的是「不装第三方 CodeGraph，沿用仓库内的
> `codegraph.py`」。独立初审指出那是**改名不是接入**：`codegraph.py` 的
> `_ALLOWED_EXTS` 只有 JS/TS/Python/JSON/Markdown，且只读固定 Git 对象、不看工作树，
> 据此宣称能查 Java/C# 客户仓库是不成立的。下面是接入之后的事实，先前那段作废。

后端是 **`@colbymchenry/codegraph` 1.6.0**（MIT，tree-sitter 语法 + Rust 内核，
本地 SQLite 索引，CLI）。唯一知道它存在的地方是 `factory/control/code_intel.py`，
三个场景共用这一层，**不许各自实现解析器**。

### 实测能力（2026-09-21，后端 1.6.0，最小两文件跨文件样例）

| 语言 | 检索 | 跨文件关系 | 构建与改造 |
|---|---|---|---|
| Java | 已验证 | 已验证 | 按项目探测 |
| Python | 已验证 | 已验证 | 按项目探测 |
| C#/.NET | 已验证 | 已验证 | 按项目探测 |
| Go | 已验证 | 已验证 | 按项目探测 |
| C/C++（可选） | 已验证 | 已验证（仅最小样例） | 未验证 |
| Rust（可选） | 已验证 | **未解析** | 未验证 |

「已验证」= `tests/test_code_intel.py` 用真实后端在最小跨文件样例上驱动断言，
**不等于在客户仓库上验证过**。构建层永远不在语言表里断言，只由
`project_conditions` 对具体项目探测。

### 三件必须由这一层纠正的后端行为（实测）

1. **`callers`/`callees` 按符号名匹配，且跨语言串味。** 混合仓库里
   `callers format_money` 同时返回了 C++ 和 Python 的调用方；Rust 的查询返回了
   C++ 的调用方。所以本层按定义所在语言过滤结果，并把丢掉的部分如实报出来——
   凭同名造一条跨语言边正是 LANGUAGE-SUPPORT.md 明令禁止的。
2. **空结果路径不认 `--json`。** 「找不到符号」和「项目没初始化」都是人话句子，
   后者退出码 1。本层分别映射成 `no_match` 与 `not_indexed`。
3. **截断无声。** `--limit 2` 对三个调用方只返回两个且不作任何提示。本层多取一个
   来判断，自己报 `truncated`。

### 结果契约

`outcome` ∈ `ok` / `no_match` / `not_indexed` / `truncated` / `unsupported` /
`backend_unavailable` / `degraded_text`。每个结果都带 `code_version`
（commit + 是否有未提交改动）、`freshness`（上次索引时间、待处理变更、是否过期）、
`unresolved`（这一层已知看不见的关系）、`dropped_other_language`。

关系层不可用时回退到文本检索，标 `degraded_text`，**绝不标成关系分析**。
`codegraph.py` 保留为回退，它的空结果带 `covers` 字段说明它只读三种语言，
免得「它不解析这门语言」被读成「代码里没有」。

### 入口

- `POST /api/v2/projects/{pid}/code-index` —— 一个动作同时建两套索引
- `GET /api/v2/projects/{pid}/code-index` —— 含 `shared_layer` 状态
- `GET /api/v2/projects/{pid}/code-conditions` —— 语言 + 工具链 + C# 的 .NET 形态
- `GET /api/v2/code-intel/capabilities` —— 三层能力与证据级别

### 已知盲区

索引落在项目内的 `.codegraph/`，它自带 `.gitignore`（`*`），所以**内容对 git 天然不可见**；
本层另外把目录名写进 `.git/info/exclude`，免得污染 worker 的 diff 或被 `git add -A` 提交。
代价是：放在 `.codegraph/` 下的代码在任何 git 形状的评审里都看不见，而跑在真实树上的
检查仍然能执行它。这条记在这里，不当作已解决。

## 不要做的

- 不新建记忆表、不新建图工具、不在场景里复制取数逻辑。
- 不把客户资料写进通用 Skill（`builtin_packs`）；客户内容进项目记忆。
- 不改 `knowledge.py`、`codegraph.py`、`scenario_memory.py`、`plugins.py` 与
  `project_routes.py`；需要新字段先找集成者。
