# webuddy-next 配置对话并发覆盖修复（2026-09-19，基线 `274de00` → 候选见推送）

只修你在 `274de00` 上复现的这一个竞态。未扩功能、未改 Skill/权限/paused/知识库/UI、未新增作业框架、未访问服务器或凭据、未部署。

## 一、修改前先复现（未改任何断言）

复现文件按你给的路径取用，已入库 `docs/acceptance/webuddy-next-2026-09-19/test_config_poll_race.py`：

```
uv run --extra codex pytest -q -p no:randomly docs/acceptance/webuddy-next-2026-09-19/test_config_poll_race.py
# 修复前：1 failed，0.84s，exit 1
#   AssertionError: ... + 正在回答
```
与你的观察一致：job 确实完成、回答确实保存过，但被 GET 用旧会话整份写回覆盖成占位文案。

## 二、原子性怎么保证，覆盖哪些读写入口

**根因不是顺序问题，是每个写入者都在做「整份 JSON 读-改-写」，而且读和写分处两个连接、中间无事务也无版本约束。** 所以缩小窗口没有意义，必须换成一致的原子更新。

新增单一写入口 `_mutate_admin_config_conv(cid, mutate)`（`agent_routes.py`，admin-config 块内）：
- 在**一个连接**里先 `BEGIN IMMEDIATE` 取写锁，**再** SELECT 最新行，把 `mutate` 应用在这份最新数据上，然后 UPDATE、提交。
- 写锁先于读取获得，所以任何并发写入者无法在「我读」和「我写」之间提交，旧副本不可能盖掉新正文。

**所有触及既有会话的写入口都改走它，无一例外：**

| 入口 | 原来 | 现在 |
|---|---|---|
| `_resolve_stale_pending`（GET 恢复） | 读旧 conv → 查 job → **把旧 conv 整份写回** | 查 job 在事务**外**完成（该查询可能阻塞，不能持锁）；随后按 `job_id` 把观测结果应用到**重新读到的最新会话**上 |
| `_update_admin_config_msg`（完成/失败/取消回调） | 读 fresh → 改 → 另一连接写 | 走 `_mutate_admin_config_conv` |
| 用户消息追加 | 读 conv → append → 整份写回 | 走 `_mutate_admin_config_conv` 合并追加 |
| pending 消息追加 | 读 conv → append → 整份写回 | 走 `_mutate_admin_config_conv` 合并追加 |
| `dispatch` 状态翻转 | 不存在 | 走 `_mutate_admin_config_conv` |

恢复逻辑里还加了一条：**在最新副本中已是终态的消息一律跳过**——那说明回调赢了竞争、已经写入真实结果，恢复不再插手。

## 三、派发窗口 vs 真实中断（你的第 3 点）

pending 必须先落库再派发，所以「job 查不到」必然有一段正常窗口。旧代码把它一律判成 `任务记录丢失`。现在用两个字段区分，不靠时间阈值：

- `boot_id`：写在 pending 消息上，值为**本进程实例**的标识（`_ADMIN_CONFIG_BOOT`，每次 `router()` 构造时生成，重启必变）。
- `dispatch`：`queued`（已落库、未派发）→ `registered`（`start_maintenance` 已返回）→ `done`（终态已写）。

判定表（仅当 `maintenance_status` 抛 KeyError 时）：

| boot_id | dispatch | 判定 |
|---|---|---|
| 与本进程**不同** | 任意 | `interrupted` / `服务重启，任务中断`（**真实重启恢复行为保留**） |
| 与本进程相同 | `queued` | **不动**，仍是 pending（正在派发中，不是丢失） |
| 与本进程相同 | `registered` | `interrupted` / `任务记录丢失` |

另外，`start_maintenance` 本身抛异常时（派发失败，永远不会有 job），当场原子地把该消息置为 `failed` / `派发失败：…`，不留一条谁也解不掉的 pending。

## 四、不把无法恢复的回答伪装成成功（你的第 4 点）

若 job 报 `completed`、但该消息正文仍是占位符 `正在回答`（即回答从未落库），**不标成功**，标 `interrupted` / `任务已结束，但回答未能保存`。成功、失败、取消（`ProviderCancelled`）、中断四态各自保持准确。

## 五、实际执行的测试与结果

全部在集成工作区 `v3-skills-icons`，`uv run --extra codex pytest -p no:randomly`：

| 项 | 结果 |
|---|---|
| 你的复现 `test_config_poll_race.py` | 修复前 **1 failed** → 修复后 **1 passed** |
| 原三条复现 `test_codex_review.py` | **3 passed**（未回退） |
| 新增定向回归 `tests/test_admin_config_race.py` | **4 passed** |
| 既有配置对话相关（`test_admin_config_conversation` + `surface_gate` + `tools` + `agent_chat_capability` + acceptance 目录） | **69 passed** |
| 更宽相关集 `-k "maintenance or admin_config or agent_chat or agent_routes or conversation"` | **86 passed** |

**变异验证**（每条守卫都确认承重，逐条改坏→复跑→还原）：

| 变异 | 变红的测试 |
|---|---|
| A 恢复改回「写回早先读到的整份 JSON」 | 你的 `test_poll_must_not_overwrite_completed_answer` + `test_reconcile_does_not_drop_a_message_added_concurrently` |
| B 去掉派发窗口守卫 | `test_dispatch_window_not_misjudged_as_interrupted` |
| C 去掉「完成但回答未保存」守卫 | `test_completed_job_without_saved_answer_is_not_reported_as_success` |
| D 去掉 `boot_id` 判别 | `test_real_restart_still_recovers_as_interrupted` |

还原后 5 passed。

**未跑后端全量、未跑前端、未跑构建**——按你的限定。依据：本轮改动只落在 `factory/control/agent_routes.py` **一个源文件**的 admin-config 块内，`git status` 可核（另两项是新增测试文件）。没有改 `store.py`、没有改中间件、没有改共享挂载路径，也没有任何前端改动，因此不构成「扩大到公共存储或其他全局路径」。

新增回归测试里的第 2、3、4 条会直接改写存储行来构造「上一个进程遗留的 pending」——这是对「重启后只剩这一行」的忠实模拟，活下来的正是它；不是为了绕过真实完成回调。你的原复现一行未改。

## 六、尚未验证的边界（我方没做，也不自行去做）

1. **真实模型**下的配置对话全链路（本轮与前轮均为受控 runner）。
2. **多进程/多 worker 部署**下的行为：`BEGIN IMMEDIATE` 依赖 SQLite 文件锁，同机多进程成立；跨主机共享存储未验证。`boot_id` 是**进程实例**级标识，多 worker 同时在线时，A worker 的 pending 在 B worker 眼里 `boot_id` 不同，会被判成「服务重启，任务中断」——**当前单进程部署下不成立，多 worker 上线前需重新设计该判别**。这条请你在服务器形态确认时一并核。
3. 真实并发压力（多管理员同时轮询同一会话）未做负载验证，只做了确定性时序复现。
4. Linux 下本修复的复验（我方为 macOS）。
5. 私有仓库导入、旧开发现场到固定地址发布，仍是原有待验收项。

`paused` 继续后置。收尾后我方停止编码，交回你做服务器主链路验收。
