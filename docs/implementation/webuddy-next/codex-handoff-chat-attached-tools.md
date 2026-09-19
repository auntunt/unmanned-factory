# 聊天内直接调用已挂靠工具（2026-09-20）

基线 `78b2d1d`（`codex/meeting-buddy-acceptance`，它已完整包含 `codex/autonomous-factory-v3` tip `dbc38c5`，我此前的 delivery gate 修复 `5f25dc7` 在其中，未丢）。
候选分支 `claude/meeting-tool-chat`，候选提交见推送。未 reset、未强推、未改动 Codex 的验收工作树。

## 调用链（先画后接，没有另造框架）

原有两段各自成立，缺的是把模型接进受控执行链：

```
用户消息
  → agent_routes.process_message（已存在）
  → conversation_tools.binding_for(store, cid, actor_id, role)   服务端生成，仅含 db_path/会话/发起者
  → [JSON 跨进程] → ConversationTools.from_binding（worker 侧重建）
  → create_server 注册 MCP 工具
      · mcp__session__attached_tools      新增：列出本角色已挂靠的包
      · mcp__session__run_attached_tool   新增：跑其中一个
  → PackTools（新增 factory/control/conversation_pack_tools.py）
      · PackStore.bindings(agent_id)      范围来自实时挂靠关系
      · PackStore.put_artifact(role=input)
      · PackStore.create_task             ← 未挂靠/他人产物/版本冻结 都在这里，复用不重写
      · pack_runtime.run_tool             ← 真实隔离执行
      · PackStore.put_artifact(role=output) + update_task
  → GET /api/v4/capability-packs/artifacts/{id}/download（已存在授权端点）
```

**模型能传的只有**：挂靠列表里的 `pack_id`、文件名、文本内容。传不了用户、会话、agent、路径、命令、URL、版本，也传不了幂等键（服务端按「会话+包+内容 sha256」算）。

## 改了什么

- 新增 `factory/control/conversation_pack_tools.py`：`PackTools.available()` / `.run()`。
- `factory/control/conversation_tools.py`：注册两个工具，执行走 `asyncio.to_thread` 不阻塞事件循环；`TOOL_NAMES` 增加两项。
- `frontend/src/conversation/SessionSkillPanel.tsx`：文案澄清（见下）。
- 测试基线更新：三处断言原本写死会话工具清单为 `{calc, export}`，改为具名基线集合，**本意未变**（普通会话不得出现配置工具）。

## 对文档四个断点的处理

1. **模型进不了受控执行链** → 上面的链路，已通。
2. **不该要求用户/模型懂内部 JSON 契约** → `attached_tools` 由服务端给出真实名称、版本、用途、输入/输出 schema、超时与大小上限；`operation_key` 我读代码后确认它其实是**幂等键**不是操作选择器，所以**不暴露给模型**，服务端自己算。
3. **「当前会话加载的 Skill · 0 项」易被误读** → 改为「本会话临时加载的 Skill」，并写明「该职能体自身已启用的规范与已挂靠的工具不在此列，它们始终生效；显示 0 项不代表没有加载」。空态同样说明。只改文案，没另造能力管理页。
4. **自动校验不等于业务语义验收** → 未改动人工反馈修订路径；修订按不同内容产生新任务、新产物，旧文件仍可下载（有测试）。

## 验证（本机真实执行，不 stub runner）

`uv run --extra codex --extra claude pytest -p no:randomly`

`tests/test_chat_attached_tool.py` **7 passed**，每条都走「服务端绑定 → JSON 序列化跨进程 → `from_binding` 重建 → 真实 MCP ClientSession 往返 → PackStore 闸门 → `run_tool` 真实隔离执行」：
- 列出并运行已挂靠包，产物经**真实 HTTP 下载端点**取回，XML 的 `<Item>` 行与 fixture **逐行一致**。
- 修订保留旧版：两次不同内容 → 两个 task、两份产物，输入 sha256 不同，旧产物仍可下载。
- 重复派发保护：同样内容第二次 `replayed=true`、同一 task_id，且 `run_tool` **调用次数为 0**。
- 未挂靠包 → 拒绝，`run_tool` **调用次数为 0**。
- 角色没有任何挂靠 → 空清单 + 拒绝，`run_tool` **调用次数为 0**。
- 解绑后拿已知 pack_id 再调 → 拒绝，清单变空，`run_tool` **调用次数为 0**。
- 工具真跑并拒绝输入 → 回报真实 `status`/`error_code`、`outputs` 为空，不是叙述性成功。

其余：相关定向 `-k "capability or pack or conversation or agent_chat or admin_config or chat"` **263 passed**。
前端：`tsc --noEmit` 0、`npm run build` 0、`vitest` **394 passed / 52 files**（新增 1 条文案澄清测试）。
未跑后端全量（按要求）。

## 未验证，交给 Codex

1. **真实模型**下模型是否会正确先 `attached_tools` 再 `run_attached_tool`，以及 `demo-input.txt` 的业务语义区分（15% 部门目标 vs 50% 单任务估计、试用 vs 采购、单方发言 vs 双方共识、缺负责人/日期保持待确认）。**本机没有跑真实模型，这部分我没有任何实测证据，也没有截图。**
2. 会议工具包（`webuddy.meeting/v1`）本身没有在我的测试里发布挂靠——我用的是仓库既有的 csv→xml 包做通用链路验证。会议包的端到端归你。
3. Linux/bwrap 下复跑；`run_tool` 在无可验证隔离时返回 `isolation_unavailable` 结构化失败（已有行为，未改），本机是 macOS seatbelt。
4. 服务器候选与生产部署。

## 边界

- 挂靠范围只认 `allowed_uses` 含 `invoke` 的绑定；版本按绑定当时冻结进 task，后续升级不改已发生的调用。
- 聊天输入上限 2 MB（小于产物存储的 16 MB），超限结构化拒绝。
- 没有硬编码任何客户名、schema、包 ID 或测试答案；包的契约由包自己的 manifest 决定。
- 没有扩大管理员配置工具范围：那两个工具仍受 `admin_config` 闸门约束，本次只新增按挂靠关系限定范围的包工具。
