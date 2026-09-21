# 真实模型验收证据（脱敏）

本文件只记录仍然存在、可核对的证据。临时控制库已删除的部分明确标为「已不可恢复」，
**没有为了补证据重跑任何付费调用**。

全部素材都是本仓库自带的合成样例（`scripts/preview_scenarios.py` 生成的
`preview/legacy-quote` 与 `preview/gp-adapter`），不含任何客户数据；
第三方端点是本机模拟端，从未调用客户生产接口。

---

## 一、信创化改造 · 真实模型切片（批次八，`fa66612`）

### 标识与版本

| 项 | 值 | 来源 |
|---|---|---|
| 切片 ID | `ee9bf92c74394839803f429feec77517` | 创建响应 |
| 执行/运行 ID | `c63cf085e3c64eb29d670755b8751c41` | `run.verified` 的 branch 名 |
| 项目 ID | `f2d23d3670ae4da2b7e277d85c4b0755` | 创建响应（本机演练项目，非客户） |
| 基线 commit | `cd6727a1865946977f74b8e5ba806d5bf7c44395` | 服务启动横幅 + `run.verified.base_sha` |
| 交付 commit | `dc60c96cffa45eee03222814e8a33635fcacf8fe` | `git.commit` / `run.verified` / 回执 |
| 集成分支 | `factory/c63cf085e3c64eb29d670755b8751c41` | `run.verified.branch` |
| 任务分支 | `factory/c63cf085e3c64eb29d670755b8751c41-task/add_dm_dialect` | `git.commit.branch` |

### 执行器与模型口径

- provider `claude`，model `claude-sonnet-5`，经用户自己的中转站
  （`ANTHROPIC_BASE_URL` 来自 `~/.claude/settings.json`，值见 STATUS 批次八）。
- 启动服务时剥掉了宿主会话的 `ANTHROPIC_*` 与 `CLAUDE_CODE_*`，
  凭据只能由 SDK 从用户自己的 settings 解析。
- **编码是真实模型**；被改的仓库是合成样例。

### 关键事件（按发生顺序）

```
provider.started   profile=planner  provider=claude model=claude-sonnet-5
                   call_id=ecb9c76b4b1d4b71ac752aba36943986  max_budget_usd=6.0
usage.recorded     profile=planner  cost_usd=0.563294
                   input_tokens=108252  output_tokens=34679
plan.created       revision=1
triage.decided     decision=human_approval  risk=high
                   reasons=["request or task scope contains a high-risk ... signal"]
（页面读到 blocking_reason.kind=approval.requested，人工批准）
usage.recorded     profile=cheap    cost_usd=0.878622  attempt=1
                   reason="small low-risk bounded task uses the economical configured profile"
check.result       name=regression  exit=0
git.commit         commit=dc60c96c...  branch=...-task/add_dm_dialect
task.completed     status=verified  commit=dc60c96c...  cost_usd=0.878622
check.result       name=regression  exit=0
run.verified       commit=dc60c96c...  base_sha=cd6727a1...
```

模型在执行阶段调用过 `Read` 工具读取 `test_db_url.py`（`tool.call` 事件，
`toolu_bdrk_01ERPNkAV8xcYUGWCufCmqg8`），说明工具调用在中转站上可用。

### 检查结果

`regression` = `python -m pytest -q -p no:randomly test_db_url.py`，**exit=0，通过**。
该检查是项目自己配置的，不是模型自评。回执 `unverified` 为空。

### 补丁与校验值

- 文件：`evidence/modernization-real-model.patch`（595 字节，就是当时导出的那份）
- SHA-256：`115684c00a4d33a32fa22f618781ce4647b2e32fd17a723c2b0f189df5130d72`
- 内容：`DIALECTS` 新增 `"dm": "jdbc:dm://{host}:{port}/{db}"`，mysql 行不变

### 费用口径

| 阶段 | profile | cost_usd |
|---|---|---|
| 规划 | planner | 0.563294 |
| 执行 | cheap | 0.878622 |
| **合计** | | **1.441916** |

预算上限 6.0（项目级 `budget_usd`）。切片视图读回的 `cost_usd` 为 `1.441916`，
与两次 `usage.recorded` 之和一致。口径是平台记账的 `known_cost_usd`，
即中转站在 usage 里报回的费用，不是我另算的估值。

### 已不可恢复的部分（如实说明）

临时控制库 `/tmp/factory-claude2/control.db` 与其工作副本已删除，因此以下**拿不回来**，
且**不会为了补证据重跑**：

- 完整的事件流与各事件的完整 payload（现存记录来自驱动脚本的日志，单条被截到 200 字符）
- `git.commit` 事件里的 `diff_hash` 只留下前缀 `46e4462b114b53cf5fbc8e124a2df4a5f1fbc9...`
- 回执 JSON 原文（只留下其中的 commit 与检查结论）
- 执行工作副本 `/private/tmp/factory-c...`

上表中的 SHA-256 是现在对**仍然存在的补丁文件**重新计算的，
不等同于当时回执里记录的 `diff_hash`（那条没有留存）。

---

## 二、接口适配 · 真实模型切片（批次九）

**口径先说清楚：编码是真实模型（`claude-sonnet-5`，经用户中转站）；第三方是本机
模拟端**——由项目自己的 `contract` 检查在检查进程内起一个 `127.0.0.1` 的
`HTTPServer`，真实走 TCP。**全程没有调用任何客户生产接口**，`base_url` 是
`http://127.0.0.1/report`，契约里 `environment` 声明为 `mock`，该标记原样进回执。

### 标识与版本

| 项 | 值 |
|---|---|
| 任务 ID | `9e9e3bd55c034dad8f13c3d78305a1b7` |
| 执行/运行 ID | `d9e67a5b2d4f4b4d97ca75ddafee587a` |
| 基线 commit | `229048fcff837b3659901c8aa999e13a2a274845` |
| 交付 commit | `3fb4e1b75c3d618a5507488189c2017fea7af68e` |
| 集成分支 | `factory/d9e67a5b2d4f4b4d97ca75ddafee587a-r2` |
| 方法版本 | `v1` / `api-adaptation@1`（服务端解析，请求里塞的 `ignored` 没有进去） |

### 关键事件（完整，本次控制库尚在，逐条取自事件表）

```
provider.started  planner  claude/claude-sonnet-5
usage.recorded    planner  cost=0.700394  in=69157   out=56208
plan.created      revision=1
triage.decided    decision=needs_clarification  risk=high  questions=2
（人工回答两个问题：只做 amount 与鉴权；metadata 保持待确认不静默丢弃；
  凭据沿用样例里的模块常量，不新建配置；第三方只用本机模拟端）
provider.started  planner  claude/claude-sonnet-5
usage.recorded    planner  cost=0.710116  in=95558   out=51900
plan.created      revision=2
triage.decided    decision=human_approval  risk=high  questions=0
（页面读到 pending_plan，人工批准）
provider.started  strong   claude/claude-sonnet-5
usage.recorded    strong   cost=0.874262  in=183756  out=50675
check.result      name=contract  exit=0
git.commit        commit=3fb4e1b75c3d  diff_hash=dac51909dfaf01bd...
check.result      name=contract  exit=0
run.verified      commit=3fb4e1b75c3d
```

### 业务响应核对

`contract` 检查的 stdout 里由检查脚本自己打出 `ADAPTATION_RESULT`，平台从**检查的
真实输出**里读回，不采信模型的自述：

| 字段 | 值 |
|---|---|
| `technical_success` | `true` |
| `business_accepted` | `true` |
| `business_completed` | **`false`**（单向上报没有完成回执渠道，不猜） |
| `mock` | `true` |
| `correlation_id` | `10174c6560fc4136829083a54dfa18a7` |
| `sanitized_response` | `{"code": "ACCEPTED", "request_id": "mock-1"}` |

脱敏有效：响应里没有 bearer 令牌。映射汇总 `{fields:2, mapped:1, unmapped:1}`，
`metadata` 按回答保持「待确认」并写明影响，**没有被静默丢弃**。

### 补丁与校验值

- 文件：`evidence/adaptation-real-model.patch`（952 字节）
- SHA-256：`06a1387dabe0c0c5c281360fa246d1c883f88b14a8a12dd3f71f69a17d0f1d1d`
- **与回执里记录的 `delivery.diff_hash` 逐字符一致**
  （`06a1387dabe0c0c5c281360fa246d1c883f88b14a8a12dd3f71f69a17d0f1d1d`）——
  这一条是信创那次拿不到的交叉核对
- 内容：`build_request` 组装 `final_headers`，加上 `Authorization: Bearer {TOKEN}`，
  并且把它放在 `**(headers or {})` 之后，调用方传入的头覆盖不掉鉴权

### 费用口径

| 阶段 | profile | cost_usd |
|---|---|---|
| 规划（第 1 版，提出 2 个问题） | planner | 0.700394 |
| 规划（第 2 版，回答之后） | planner | 0.710116 |
| 执行 | strong | 0.874262 |
| **合计** | | **2.284772** |

预算上限 6.0。任务视图读回的 `cost_usd` = `2.284772`，与三条 `usage.recorded` 之和一致。

### 这一轮真实模型暴露出来的两个洞（已修）

1. **`needs_clarification` 三个业务模块都没映射**。模型在动手前提问是对的行为，
   而那一刻整个任务视图直接 409「无法映射到任务状态」——不是「卡住」，是**读都读不了**。
   三个模块各补一行映射到 `waiting`。
2. **适配面没有回答问题的入口**。补了 `POST /tasks/{id}/clarify`（与 `approve`
   同样手写一次 `gate.require('continue')`），视图补 `pending_questions` 与
   `clarification.requested` 阻塞原因。

维护与信创两个面现在视图可读、能看到阻塞原因，但**仍然没有各自的回答入口**——
本轮按范围只给适配补了，这一条如实记在未验证项里。
