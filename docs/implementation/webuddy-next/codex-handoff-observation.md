# webuddy-next 否定观察有效性补修（2026-09-19，基线 `6f01729` → 候选见推送）

只修你在 `6f01729` 上复现的这一条确定性竞态。保留原子 mutator 与既有 maintenance job，未新增框架、未改公共存储、未动前端/UI/paused，未访问服务器或凭据、未部署。

## 一、先复现（未改你的断言一字）

复现文件原样取用，已入库 `docs/acceptance/webuddy-next-2026-09-19/test_dispatch_observation_race.py`：

```
uv run --extra codex pytest -q -p no:randomly docs/acceptance/webuddy-next-2026-09-19/test_dispatch_observation_race.py
# 修复前：1 failed，1.05s，exit 1
```

## 二、观察失效机制

**你的诊断成立。** 上一轮我把 job 状态查询移到事务外是对的（不能持写锁调用可能阻塞的外部逻辑），但只保证了**写不互相覆盖**，没保证**事务外得到的判断在写回时仍然成立**。

关键在于两类观察的性质不同：
- **肯定观察**（job 报 completed/failed/cancelled/interrupted）是**单调**的，终态不会回退，过一会儿再用仍然正确。
- **否定观察**（「这个 id 下没有 job」）**只对观察那一刻的派发阶段成立**。一条正在派发中的消息本来就还没有 job，片刻之后它就会有。

所以补的是否定证据的有效性条件，而不是再查一次缩小窗口：

1. 采集观察时，**连同记录该消息当时的派发阶段** `dispatch`：
   `observed[jid] = (状态或 None, 观察时的 dispatch)`
2. 在原子事务内应用时，若 `job is None` 且该消息**当前的 `dispatch` 与观察时不同**（典型是 `queued → registered`），则这条否定证据描述的是它已经离开的阶段——**不使用，跳过，留待下次轮询重新观察**。
3. 阶段未变时，原判别照旧：`boot_id` 不同 → `服务重启，任务中断`；同进程且 `queued` → 仍是 pending（派发窗口）；同进程且 `registered` → `任务记录丢失`。

**真实丢失与重启恢复没有被无条件放过**：只有「阶段在观察之后前进了」这一种情况才作废证据；阶段稳定的缺失仍然按原逻辑终结。肯定观察不加该条件，因为终态单调，无需失效。

改动位置：`factory/control/agent_routes.py` 的 `_resolve_stale_pending`（采集处与 `apply` 内 `job is None` 分支）。仅此一个源文件。

## 三、真实测试结果

集成工作区 `v3-skills-icons`，`uv run --extra codex pytest -p no:randomly`：

| 项 | 结果 |
|---|---|
| 你的新复现 `test_dispatch_observation_race.py` | 修复前 **1 failed** → 修复后 **1 passed** |
| 你那组 19 条（`test_config_poll_race` + `test_codex_review` + `test_admin_config_race` + `test_admin_config_conversation`）+ 新复现 | **20 passed** |
| 配置会话定向集（上面四项 + `surface_gate` + `tools` + `agent_chat_capability` + 整个 acceptance 目录） | **75 passed** |

新增一条确定性回归（本仓自有，不依赖你的文件）：`tests/test_admin_config_race.py::test_stale_missing_job_evidence_is_invalidated_by_stage_change`——单线程构造，`maintenance_status` 抛 KeyError 的同时把该消息 `dispatch` 由 `queued` 推进到 `registered`，断言消息仍为 pending。

**一次定向变异**（按你的要求没做大轮变异）：把有效性条件改成恒假 → 你的复现与上面这条自有回归**双双变红**；还原后 6 passed。守卫承重。

**未跑后端全量、未跑前端与构建**——按你的限定。改动仍只落在 `factory/control/agent_routes.py` 一个源文件（另两项为测试文件），未触及 `store.py`、中间件、共享挂载或任何前端。

## 四、边界

1. **多 worker 限制的表述已按你的更正改正**。此前我在 STATUS 里写成「多 worker 下所有跨 worker GET 都会误判」，**过头了**。准确表述：`boot_id` **只在 job 查询缺失时**才被检查，因此只有「跨 worker 读取 + 恰好查不到该 job」的组合才会被判成重启中断，并非所有跨 worker 读取。当前生产单进程（你只读确认 `factory-web.service` active、MainPID 1468377、仍 `4c56632`、无子进程），该限制继续后置登记，本轮不扩分布式架构。
2. 本轮这条 P1 与多 worker 无关，**单进程即可发生**，已按此定位修复。
3. 仍未在本轮完成、归你的：真实模型配置对话、私有仓库导入、Linux 新候选复验、交付主链验收。以上定向结果**不等于全平台验收**。

补修完成后我方停止编码，上线与真实主链验收交回你。
