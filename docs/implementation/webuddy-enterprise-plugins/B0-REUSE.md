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

## 代码关系查询 = `factory/control/codegraph.py`（已在仓库内，已接线）

不装第三方 CodeGraph。理由：仓库里已有一个确定性的图索引，直接读 Git 对象、
不看工作副本，带 `SCHEMA_VERSION`/`PARSER_VERSION`、有大小上限与敏感路径过滤，
且 HTTP 面已经接好（`project_routes.py`）：

- `POST /api/v2/projects/{pid}/code-index` 建快照（按 `base_branch` 的 commit 固定）
- `GET  /api/v2/projects/{pid}/code-index` 读快照元数据与新鲜度
- `GET  /api/v2/projects/{pid}/code-search?q=&limit=` 定位（返回 file:line）
- `GET  /api/v2/projects/{pid}/code-graph?node=` 调用关系切片

再装一套外部工具会带来许可证、遥测、全局执行器配置三个本轮不必要的风险，而
SHARED.md 明确允许「缺工具时可用符号/文本检索继续工作」。快照过期时用
`baseline_sha` 对比判新鲜度；**索引没覆盖到的（动态 SQL、私有二进制、外部数据库对象）
按未验证记，不要写成"仓库里没有"**。

## 不要做的

- 不新建记忆表、不新建图工具、不在场景里复制取数逻辑。
- 不把客户资料写进通用 Skill（`builtin_packs`）；客户内容进项目记忆。
- 不改 `knowledge.py`、`codegraph.py`、`scenario_memory.py`、`plugins.py` 与
  `project_routes.py`；需要新字段先找集成者。
