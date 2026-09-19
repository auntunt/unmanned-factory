# 普通聊天回答完成态修复（2026-09-19，基线 `f5b238c` → 候选见推送）

只修你在服务器现场发现的普通无项目 do 聊天完成态问题。未改 admin-config，未重写聊天模块，未新增框架。

## 一、现场与根因

现场证据 `live-acceptance.json` 的 `checks.quote_real_model.messages` 里，同一个 `job_id` 下有**两条** assistant 消息：一条 `pending`「正在回答」，一条 `completed` 带真实回答。

根因两处，都在 `agent_routes.py` 的 `process_message` 普通分支：
1. **完成时是追加新消息**，不是结束原 pending（`agents.append_message(... status='completed' ...)`）。原 pending 永远留着。
2. **pending 在派发之后才落库**：`_append_pending` 原本排在 `service.start_maintenance` **之后**。快完成的 job 会先写完回答，随后那条 pending 才被追加，同样永久残留。

前端 `AgentChatPage.tsx:79` 以 `messages.some(pending/running)` 控制 typing 与 2s 轮询，所以「正在回答」永不收起，刷新亦然。

## 二、改法（复用 AgentStore 与既有 maintenance，两套存储不混用）

明确前提：普通聊天走 `AgentStore` / `agent_conversations`，与 admin-config 的 `admin_config_conversations` 是**两个存储路径**，因此**没有照搬** admin-config 的实现。`AgentStore.append_message` 本身早已是 `BEGIN IMMEDIATE`，存储原子性本来就有，缺的是生命周期。

新增两个 `AgentStore` 方法，沿用该类既有的 `BEGIN IMMEDIATE` 读-改-写写法：

- `resolve_answer_message(cid, job_id, status, content, **extra)`：把该 job 的 pending/running assistant 消息**就地**置终态并写入真实正文；**找不到则追加**——回答绝不能因为占位符不在而丢失。
- `resolve_messages_from_dead_process(cid, live_boot_id)`：只结束 `boot_id` 与当前进程不同的 pending/running 消息，置 `interrupted`/`服务重启，回答中断`。

`process_message` 侧：
- `_append_pending` 记录 `boot_id=_CHAT_PROCESS_BOOT`（模块级、每进程唯一）。
- **`link_answer_job` 与 `_append_pending` 双双移到 `start_maintenance` 之前**：回执绑定与占位符都必须先落库，快完成的 job 才有东西可结束。
- 完成 → `resolve_answer_message(..., 'completed', result.text, usage=...)`。
- 失败/取消 → 区分 `ProviderCancelled`：`cancelled`/`回答已取消`，否则 `failed`/`回答失败：…`。
- `start_maintenance` 抛异常 → 当场置 `failed`/`派发失败：…`，不留无人能解的 pending。

**重启终态刻意用了比 admin-config 更简单、更安全的机制**：只看消息上记录的进程标识，**完全不查 job 表**。因此不存在「事务外取得的否定观察过期」那类竞态——那正是上一轮 admin-config 踩过的坑。GET 不查 job 状态、**不把任何 pending 标成功**；没有 `boot_id` 的老消息一律不动（判断不了就不猜）。

## 三、真实测试结果

新增 `tests/test_chat_answer_lifecycle.py`（用现有 fake runner 走真实发消息 → maintenance 完成的全路径）：

| 测试 | 旧代码 | 新代码 |
|---|---|---|
| `test_completed_answer_leaves_no_active_pending` | **红** | 绿 |
| `test_failed_answer_leaves_no_active_pending` | **红** | 绿 |
| `test_cancelled_answer_has_its_own_terminal_state` | **红** | 绿 |
| `test_idempotent_replay_still_points_at_the_original_user_message` | 绿 | 绿 |
| `test_answer_left_by_a_dead_process_is_ended_on_read` | （新机制，旧代码无此字段） | 绿 |
| `test_read_does_not_touch_an_answer_from_the_live_process` | （同上） | 绿 |

**如实说明**：红→绿的是前 3 条。幂等回执那条**在旧代码上本来就是绿的**——它是防回归护栏，证明我把 `link_answer_job` 提前到派发之前没有破坏「回执仍对应原用户消息」，不是红→绿示例。我最初把它写错了字段名（`client_key` 而非 `idempotency_key`）导致 422，那次红不算数，改对后重跑确认。

其余（`uv run --extra codex pytest -p no:randomly`）：
- 聊天/配置相关定向（`agent_chat_capability` + `agent_routes` + `agent_feedback` + `agent_service_review` + 本文件 + admin-config 两组 + 整个 acceptance 目录）：**98 passed**。
- 更宽 `-k "agent or conversation or chat or maintenance or skill"`：**288 passed**。
- 前端：`tsc --noEmit` 0、`npm run build` 0、`vitest` **389 passed / 50 files**。

前端**未改源码**——它本就按 pending/running 判定，后端修好即闭环。按你的要求补了定向测试
`AgentChatPage.test.tsx › 停止 typing 与轮询`：同一条消息由 pending 变终态后，`.cv-typing` 消失，且再推进 6 秒轮询调用数不再增长。

未跑后端全量（按你的限定）。

## 四、边界

1. `_append_pending` 是普通聊天与**维护整理（maintain）**共用的。我给它加了 `boot_id`，因此重启恢复也会作用于 maintain 的 pending——这是正确方向且只在真实重启后触发，但**属于本次改动对相邻路径的影响，明确告知**。maintain 路径自身的「完成时追加新消息 + pending 在派发后落库」是**同一形状的既有问题，本轮按范围未修**，请你决定是否另开一单。
2. 没有 `boot_id` 的历史消息（本次上线前产生的）不会被重启恢复，会继续停在 pending。不猜、不批量改写历史数据。
3. 本轮未验证：真实模型下的取消路径、真实重启现场。
