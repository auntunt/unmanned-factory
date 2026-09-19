# 普通成员日常聊天入口打通（2026-09-19，基线 `51e518a` → 候选见推送）

第二笔小提交。只打通已授权成员自己的「无项目 do 日常会话」，不做多人协作新功能。

## 一、先复现（未改你的断言）

`docs/acceptance/webuddy-live-f5b238c/test_member_daily_chat.py`（原样入库）：
```
uv run --extra codex pytest -q -p no:randomly docs/acceptance/webuddy-live-f5b238c/test_member_daily_chat.py
# 修复前：1 failed（403 此操作需要管理员权限）→ 修复后：1 passed
```

## 二、改了什么（三处，都很窄）

### 1. 中间件窄白名单（`app.py`）
**逐条列出**成员完成自己一段日常会话所必需的 POST，不放开 `/api/v4`，也不放开整个 `/conversations` 前缀：
- `/api/v4/agents/{aid}/conversations`
- `/api/v4/conversations/{cid}/(messages|attachments|calc|export)`
- `/api/v4/maintenance-jobs/{job_id}/cancel`

**刻意不在其中**：`/conversations/{cid}/retry`（只属于维护对话）、`/conversations/{cid}/project`（项目绑定必须走项目授权）、`/agents` 创建、draft/apply/rollback、skills、modules、admin-config。
下载与读取本就是 GET，沿用各自既有的归属校验。

### 2. 服务端强制 do / no-project（`agent_routes.py` 创建会话）
非管理员创建会话时，`mode != 'do'` 或带 `project_id` 一律 403。**不是靠 UI 隐藏**：maintain 会改角色，项目型会话必须走项目授权，两者都不从这道口子过。

### 3. 补上 job 取消的归属校验（`agent_routes.py`）—— 你点名要核的那条
`service.cancel_maintenance(job_id, actor)` **接收 actor 但完全没用它**。此前靠中间件挡住所有成员写入才没暴露；一旦放成员取消自己的回答，不补校验就是越权。
已在路由入口补上，判据与**同文件 `GET /maintenance-jobs/{job_id}` 既有的那条完全一致**（`actor_id not in (0, 当前用户) and role != 'admin'` → 403），复用既有契约，没有发明新规则。

## 三、可见性依据（按你的要求记录，不自行发明权限模型）

`GET /api/v4/agents` 对**任何已登录用户**返回全部职能体（`agent_routes.py:190-191`），仓库里**不存在按成员划分的职能体可见性模型**。因此这道窄口的口径是：成员可以对**他本就能看到的**职能体开启自己的日常会话。我没有新增职能体级可见性规则——那属于新权限模型，超出本单。会话列表本身已按 `actor_id` 过滤（`conversations` 端点），跨用户会话读写各端点也都已有归属校验。

## 四、真实测试结果

新增 `tests/test_member_daily_chat_access.py`，正向 2 条 + 反向 5 条：

| 方向 | 测试 |
|---|---|
| 正向 | 成员完成自己一段会话：创建 → 发消息 → 附件 → calc → 导出 → 下载 → 读取，全部 2xx |
| 正向 | 成员能取消**自己**的回答 job |
| 反向 | 成员**不能**取消他人的 job（403） |
| 反向 | 成员**不能**读/发/导出他人的会话（403×3） |
| 反向 | 成员**不能**开 maintain 会话、不能带 `project_id`（403×2） |
| 反向 | admin-config、创建职能体、rollback、modules 仍 403 |
| 反向 | `retry` 与 `project` 绑定仍 403 |

**变异验证**（两处安全关键点）：
- 去掉 job 取消归属校验 → `test_member_cannot_cancel_another_users_job` 变红。
- 白名单放宽成 `/api/v4` 前缀 → `test_member_still_blocked_on_admin_config_and_other_v4_writes` 与 `test_member_cannot_retry_or_bind_a_project_on_a_conversation` 双双变红。
还原后 8 passed（含你的复现）。

### 授权边界契约测试
`tests/test_app_route_contract.py` 抓到了这次改动——**这是它该做的**。我按仓库既有约定（R2 那轮为会话 Skill 建的 `_normalize_session_skill_whitelist`）新增了 `_normalize_member_chat_whitelist`，把本次**已复核**的新增规范化掉后，哈希**仍等于原始基线**，即中间件其余部分逐字节未变。
**没有改基线哈希**——那会废掉这道守卫。并已验证守卫仍然有效：把白名单里的 `cancel` 偷偷改成 `(cancel|retry)`，两条契约测试立刻变红，还原后 4 passed。

### 定向集
`-k "agent or conversation or chat or auth or permission or role or session_skill or admin_config or governance or team or route_contract"`：**433 passed**。
**全量**：`uv run --extra codex pytest tests -m "not smoke" -q -p no:randomly` → **2617 passed / 0 failed / 20 skipped**，退出码 0，771.81s。
你说本单不必重跑全量，我仍补跑了一次——改动落在**全局权限中间件**上，按上一轮双方认可的口径（中间件与共享挂载路径值得一次全量）。对照：本轮两笔提交前基线 2599，增量全部为本轮新增回归，无既有用例退化。

前端未改动，未重跑前端。

## 五、边界

1. 成员可对任何他能看到的职能体开日常会话，依据见第三节；若产品要收窄，需要**新增**职能体可见性模型，不在本单。
2. 未验证：真实服务器上的成员身份现场、真实模型下的成员会话。
3. `service.cancel_maintenance` 内部仍不校验 actor；本次在路由入口挡住。若将来有别的调用方直接用该服务方法，需各自校验——**已登记，未改服务层签名以免牵动其他调用点**。
