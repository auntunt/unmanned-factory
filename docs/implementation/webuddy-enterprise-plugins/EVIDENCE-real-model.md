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
