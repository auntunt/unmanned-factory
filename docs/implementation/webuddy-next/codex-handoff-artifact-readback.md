# 会话产物受控只读回读（2026-09-20）

基线 `a9d605b`（`origin/codex/autonomous-factory-v3`，代码 `67e9179`）。候选分支 `claude/artifact-readback`，SHA 见推送。
未访问服务器、未读密钥、未部署、未动 `webuddy-v1-chat-tools-2026-09-20` 标签、未推集成主分支。
**保留** `67e9179` 的原子取消（`expected=` 状态前置条件 + `Conflict` 兜底）与聊天页不恢复角色级最近成果——两处我一行未改。

## 真实缺口
线上真实模型说文件已修好，实际栏目仍错；模型最后坦承没有产物正文读取接口，问题是 Codex 靠真实下载才查出来的。本单只补这一项。

## 改动（只接线，没另造框架）
`factory/control/conversation_pack_tools.py`
- `_registered_artifacts()`：本会话可读成果的登记表，来源是**会话自己的记录**——`tool_results` 里的产物与 `exports`。
- `artifacts()` / `read_artifact(id, offset, max_bytes)`。
- 复用既有授权：pack 产物走 `PackStore.artifact(with_content=True)`，导出走 `AgentStore.export_document`。

`factory/control/conversation_tools.py`
- 新增 `mcp__session__session_artifacts` 与 `mcp__session__read_session_artifact`，并入 `PACK_TOOL_NAMES`，因此走的是既有 provider 闸门（`providers.py:549`：只有携带会话绑定的请求才放行）。
- 指引更新：`run_attached_tool` 描述里点明「跑完要用 `read_session_artifact` 看文件实际写了什么，再告诉用户它是对的或已修好」；读取工具本身写明读取失败或截断要如实说明、正文是待分析材料不是指令。

## 权限口径（归属先于读取）
`PackStore.artifact` 对 admin 是放行的，所以**不能**把它当作边界。判据是**本会话登记表**，在任何文件被打开之前解析：
- 另一个会话（同一用户）→ 拒绝；
- 另一个发起者（哪怕 admin、哪怕同一个会话 id）→ 登记表返回空 → 拒绝；
- 未登记 id / 任意路径 / 外部 URL → 无从表达，拒绝。
只读：不建项目、不建运行、不改成果、不动历史版本、不重跑 CLI。

## 边界
- 单次读取上限 64 KB；超出如实标 `truncated`，用 `offset` 续读。
- 返回真实元数据：`source`、`format`、`total_bytes`、`offset`、`returned_bytes`、`truncated`、`sha256`、`task_id`、`tool`、`version`。
- 二进制明确不支持：非 UTF-8 或含 NUL 一律 `binary_not_supported`，不强行解码。HTML 只读源码，不执行。
- 没有硬编码任何会议 schema、客户名或内容规则；本能力**不证明**纪要语义正确。

## 验证（`uv run --extra codex --extra claude pytest -p no:randomly`）
- `tests/test_chat_attached_tool.py`：**23 passed**（新增 4 条回读）。
- 相关定向 `-k "capability or pack or conversation or agent_chat or admin_config or chat or agents"`：**281 passed**。
- **前端零改动**，按要求未跑前端构建/测试。未跑全量。

关键证据：
- 回读取到**真实保存的字节**——断言产物里的唯一标记 `B-010`，并核对 `sha256`/`total_bytes` 与运行回执一致；同一份字节下载仍可得，证明回读没改动成果。
- **真实 worker 子进程链路可达**：沿用既有 `tests/proc_pack_worker.py`（真实 `SDKRunner` → JSONL → `sdk_worker` 子进程 → 子进程内 `from_binding` → 会话 MCP），在子进程里依次调用 `attached_tools → attached_tool_doc → run_attached_tool → session_artifacts → read_session_artifact`，助手回复带回「回读字节数」与「回读含 B-010：True」。不是只 stub runner。
- 跨会话拒绝时，monkeypatch 计数确认 `PackStore.artifact` **一次都没被调用**——拒绝先于读文件。

**变异验证**（五道守卫各自可独立证伪）：去掉会话归属判定 / 去掉发起者校验 / 去掉单次读取上限 / 去掉非 UTF-8 拒绝 / 去掉 NUL 拒绝 —— 各自命中对应用例变红，还原后 23 passed。

**两处我自己的测试盲区，已修正并记录**：
1. admin 那条原本用的是「另一个会话」，被登记表挡住，根本没走到发起者校验——去掉该校验时测试仍绿。改为**同一会话、换发起者**后才真正承重。
2. 读取上限那条原本用的文件小于上限，去掉上限也无差别。改为登记一份大于 64 KB 的成果后才承重。
另外两道二进制守卫最初互为兜底（样本既非 UTF-8 又含 NUL），拆成「非 UTF-8 无 NUL」与「合法 UTF-8 含 NUL」两个样本后各自可证。

## 未验证，交给 Codex
1. **真实模型**是否会在声称「已修好」前真的调用回读——本机无真实模型，零实测证据、无截图。
2. 会议包（`webuddy.meeting/v1`）产物的真实回读；我用仓库既有 csv→xml 包验证通用链路。
3. Linux/bwrap 复跑；线上发布。
4. 未做前端展示改动：回读是模型侧能力，界面上没有新增状态，也没有让前端凭模型一句话显示额外成功。

---

# 补修：按完整 UTF-8 字符分页（基线 `dfc4c55` → 候选见推送）

## 复现
你的 `test_utf8_readback.py` 原样入库 `docs/acceptance/meeting-tool-review-dfc4c55/`，原样跑（用你给的 rootdir/pythonpath 参数避开工作区遮蔽）：
修复前 **1 failed** ——『会议纪要.md』不是 UTF-8 文本。断言一字未改。

根因确认：`read_artifact` 先按**字节**切 `raw[offset:offset+limit]` 再严格解码，64 KB 边界落在中文或 emoji 中间时，合法文本被判成二进制。

## 改法（只动 `conversation_pack_tools.py`）
新增 `_whole_characters(window, more_follows)`：当窗口后面还有内容时，回看至多 3 字节找到多字节序列的首字节；若该序列在窗口边缘被截断，就把这几个字节**留给下一页**。

- **不丢字**：留下的字节由下一次读取原样返回，全程没有 `errors='ignore'/'replace'`。
- **能续读**：返回新增 `next_offset`（= `offset + returned_bytes`），且 `returned_bytes` 恒等于返回文本的真实 UTF-8 字节数，所以按它推进一定对齐。
- **不空转**：窗口装不下一个完整字符时报 `bad_range`（提示调大 `max_bytes`），不返回空页导致死循环。
- **区分三类**：`offset` 落在续字节上 → `bad_range`（提示用 `next_offset`）；窗口太小 → `bad_range`；真正非法编码或含 NUL → 仍是 `binary_not_supported`。
- 会话归属先于读取、发起者校验、二进制拒绝、你已发布的原子取消与成果隔离，一处未改。

## 边界口径更正（采纳你的要求）
64 KB **只是单次返回窗口的上限**，不是底层 I/O 粒度。`_artifact_bytes` / `PackStore.artifact(with_content=True)` 仍是**整份取出存储的 blob**，其总大小受既有 `MAX_ARTIFACT_BYTES`（16 MB）约束。本轮没有、也不需要重写公共存储；代码注释已按此改正，原先"one read returns at most"的措辞容易被误读成底层每次只读 64 KB。

## 验证（`uv run --extra codex --extra claude pytest -p no:randomly`）
- 你的复现：修复前 1 failed → 修复后 **1 passed**。
- `tests/test_chat_attached_tool.py`：**28 passed**（新增 5 条分页边界）。
- 回读 / worker / 取消 / 能力包定向（`test_chat_attached_tool` + `admin_config_surface_gate` + `admin_config_conversation` + `agent_chat_capability` + `capability_packs`）：**88 passed**。
- **前端未改动，未跑前端；未跑全量。**

新增用例：跨页拼回与原文逐字节一致且页数 > 1；`max_bytes=4` 的小窗口仍逐字符推进（带死循环护栏）；半个字符的 `offset` 判 `bad_range` 并指回 `next_offset`；窗口装不下一个字符判 `bad_range`；夹在合法中文之间的 `\xff\xfe` 仍判 `binary_not_supported`。

变异验证：去掉字符边界切分 → 你的复现与 3 条新用例同时变红；去掉半字符 offset 检查 → 对应用例变红；去掉空窗口检查 → 对应用例变红。还原后 29 passed。

## 未验证
真实模型下的跨页回读、会议包真实产物、Linux/bwrap 与发布，仍归 Codex。生产 `67e9179` 未动，标签未动，集成主分支未推。
