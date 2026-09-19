# 聊天调用已挂靠工具：Codex 复核 6f1f5e7 三项修复（2026-09-20）

基线 `6f1f5e7`（分支 `claude/meeting-tool-chat`）。候选见推送。未碰服务器/凭据/生产，未改 Codex 复现断言。

## 先复现
`docs/acceptance/meeting-tool-review-6f1f5e7/test_cancel.py`（你的原件，原样入库）：修复前 **1 failed**（`succeeded != cancelled`）→ 修复后 **passed**。

## R1 模型缺少真正的文件内容契约

`available()` 原来把 `tool.input_schema` 当成 `content` 的格式给模型——那其实是 **CLI 包装协议**（`input_dir/output_dir/inputs`）。现场模型连猜五次失败，是这个字段误导的。

改法（通用，按已发布版本限定，不写死任何客户 schema）：
- 该字段改名 `invocation_schema`，并注明它描述平台如何启动程序、**不是**文件格式。
- 新增 `content_contract`：从**该已发布版本自带的文档**（README/*.md/*.txt/*.rst，排除 `tool/`）取有界摘录；附 `content_contract_files` 与 `content_contract_truncated`。
- 新增会话工具 `mcp__session__attached_tool_doc`：按 pack 读该版本文档全文（限已挂靠的包、限该版本实际附带的文档）。
- 补上 `support_matrix`。
- `max_input_bytes` 改为 **min(聊天上限, 包 manifest 的 `permissions.max_input_bytes`)**——原来一律报 2 MB，而会议包只收 512 KB，只是把失败推后。

平台不知道、也不编码任何特定客户的 schema：给什么契约完全由包自己的文档决定。

## R2 结果没接入本次会话

新增 `AgentStore.add_tool_receipt(cid, actor_id, receipt)`（与 `add_export` 同款 `BEGIN IMMEDIATE` 原子写、同款会话归属校验）。`PackTools` 每次执行结束（成功/失败/取消/崩溃）都把**存储里的任务回执**写到这个会话：task_id、冻结版本、真实状态、输入摘要、真实产物 id 与下载路径。

- **不解析模型自述**：回执来自 `packs.task()`，不是模型写的链接。
- **会话归属**：挂在 `agent_conversations` 行上，另一个会话读不到（有测试）。
- **刷新恢复**：随会话 GET 返回。
- 前端 `AgentChatPage` 渲染 `tool_results`：成功给可点击下载（指向既有 `/api/v4/capability-packs/artifacts/{id}/download`，沿用既有产物权限），失败/取消显示真实错误，**不显示下载链接**。
- 同一 task 重跑就地更新，不堆重复；上限 20 条。

## R3 取消未传到执行、取消被覆盖成功

三处都接上了，而且**各自可独立证伪**：
1. `create_task` 现在带 `job_id`（与任务同事务落库），你的 cancel 端点可寻址。
2. `run_tool` 传入 `_PersistedCancel`：它读**已落库的** `cancel_requested`。聊天工具跑在 worker 里，协调进程的内存 Event 不存在；cancel 端点是先写库再动内存事件，所以库行才是跨进程可靠的真相来源。
3. 收尾时若已落库取消而工具仍报成功 → **降级为 cancelled**，不覆盖用户的取消意图。
4. `run_tool` 抛异常 → 写 `failed` / `executor_crashed` 终态并留回执，不会永远 running。

## 验证

`uv run --extra codex --extra claude pytest -p no:randomly`
- `tests/test_chat_attached_tool.py` + 你的复现目录：**18 passed**。
- 相关定向 `-k "capability or pack or conversation or agent_chat or admin_config or chat or agents"`：**275 passed**。
- 前端：`tsc --noEmit` 0、`npm run build` 0、`vitest` **395 passed / 52 files**。
- 未跑后端全量（按要求）。

**真实 worker 子进程边界**（新增 `tests/proc_pack_worker.py` + `test_attached_pack_runs_through_the_real_worker_subprocess`）：聊天端点 → 真实 `SDKRunner` → JSONL → `sdk_worker` **子进程** → 子进程内 `from_binding` 重建 → 会话 MCP 工具（`attached_tools` → `attached_tool_doc` → `run_attached_tool`）→ 真实包执行 → 回执落到该会话的 SQLite 行 → 真实 HTTP 下载到 `<Item` 内容。只替换 `claude_agent_sdk.query`，runner 与进程通信都是真的。

**变异验证**（每条守卫都确认承重）：
| 变异 | 变红 |
|---|---|
| 不暴露 content_contract | R1 契约测试 |
| 不写会话回执 | R2 三条 + 子进程边界测试 |
| 不把 cancel 传给执行器 | `test_the_executor_actually_receives_a_cancel_handle` |
| 收尾不看已落库取消意图 | `test_a_tool_that_ignores_cancel_is_still_not_reported_as_success` |
| 异常不收敛终态 | `test_an_executor_crash_reaches_a_terminal_state_not_running_forever` |

**过程中的一次自我纠正**：R3 那两条防线最初互为兜底——单独去掉任一条，你的取消复现仍然绿，所以没有测试能单独证明它们承重。补了「执行器确实拿到 cancel 句柄」和「包忽略 cancel 时也不得报成功」两条能区分的用例后，三条防线才各自可独立证伪。

## 口径更正
上一份交接说 7 条测试「每条完整跨进程链路」——**不成立**，已在原文标注更正。`_worker_tools` 是同进程 JSON 往返，不是子进程。原 7 条保留（它们验证绑定可序列化、MCP 往返、闸门与真实包执行），进程边界另由上面那条新测试覆盖。

## 未验证，交给 Codex
1. **真实模型**是否会按新顺序先读 `attached_tool_doc` 再构造文件，以及 `demo-input.txt` 的业务语义区分。本机没有真实模型，这部分我零实测证据、无截图。
2. 会议包（`webuddy.meeting/v1`）端到端仍未在我的测试里发布挂靠；我用仓库既有 csv→xml 包验证通用链路。
3. `CapabilityPanel` 的「角色最近任务」仍是角色级视图（旧会话成果会出现在那里）。本次把**本会话**的可信回执做进了聊天区；该面板的语义是否要一并收窄，留给你定，我没有扩范围去改它。
4. Linux/bwrap 复跑；服务器与生产部署。
