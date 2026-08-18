# PRD: unmanned-factory 全流程展示前端

## 目标
为 unmanned-factory 构建一个可演示的 Web 前端，完整展示从「用户提需求」到「代码合并上线」的九步流程，让非技术人员能看懂自动化工厂在干什么。

## 交付物
- [ ] React SPA，独立前端服务（Vite）
- [ ] 后端 API 层（Python Bottle，复用现有 audit.db + queue/）
- [ ] Caddy 配置更新（反代新前端路径）
- [ ] 部署脚本 + systemd 单元

## 技术约束
- 前端：React 18 + Vite + TailwindCSS + shadcn/ui（或 Ant Design）
- 后端：Python 3.11 + Bottle（新增 `/api/*` 路由，返回 JSON）
- 数据源：
  - `audit.db`（task_attempt / supervisor_verdict 表）
  - `queue/inbox|running|done|needs-human|blocked/*.yaml`
  - `queue/log/*.jsonl`（可选，用于实时流式日志）
- 轮询：前端每 60 秒请求一次 `/api/tasks` 和 `/api/task/{id}/attempts`
- 运行环境：`43.153.76.85`，Caddy 反代 `/app` → 前端 Vite dev server（或打包后的静态文件）

## 完成标准（验收条件）
- [ ] 能看到任务列表，按状态分组（inbox / running / done / needs-human / blocked）
- [ ] 点击任务进详情页，看到轮次时间线（attempt 1 → 2 → ... → merged）
- [ ] 每轮显示：用的模型、花费、token 数、耗时、resolution（pass/fail/escalated/merged）、监工判词
- [ ] 费用统计：单任务总花费、全局累计花费
- [ ] 实时更新：任务状态每分钟自动刷新（轮询），running 状态显示「进行中」动画
- [ ] 删掉现有 dashboard（`factory/dashboard.py` 及相关文件）和 `/console/*` 路由

## 九步流程映射到数据模型

| 展示步骤 | 数据来源 | 前端呈现 |
|---------|---------|---------|
| 1. 需求确认 | `queue/{state}/{task_id}.yaml` 的 `prompt` 字段 | 卡片：用户原始需求 + 系统理解（PRD 占位，目前无） |
| 2. 功能拆分 | 当前无（单任务无 WBS），占位显示「单一任务，无需拆分」 | - |
| 3. 技术选型 | `task_attempt.harness` + `harness_version` | 标签：claude_code 2.1.234 + bwrap |
| 4. 系统设计 | `yaml` 的 `checks` 字段 | 验收标准列表 |
| 5. 子代理执行 | `task_attempt` 表，每条是一轮 | 时间线：attempt_no 1→2→...→9，每轮显示 model/tokens/cost |
| 6. 单元测试 | `supervisor_verdict` where `role='regression'` | Pass/Fail + claims（失败原因） |
| 7. 集成测试 | 当前无独立集成阶段，占位 | - |
| 8. 冒烟测试 | 当前无独立冒烟阶段，占位 | - |
| 9. 部署 | `task_attempt.commit` + `resolution='merged'` | commit SHA + 分支名 + 「已合并」徽章 |

简化版（实际有数据的五步）：
1. **需求** → YAML prompt
2. **闸门检查** → `oracle_class` + `class_reason`（C 级会拦）
3. **执行轮次** → `task_attempt` 多条，每轮一条
4. **监工判收** → `supervisor_verdict`（scope/regression/risk 三个监工）
5. **合并** → `resolution='merged'` + commit

## 待定假设（风险项）
- 无 PRD 生成模块 → 需求确认步骤暂时只显示用户原始 prompt
- 无 WBS 拆解 → 功能拆分步骤占位或隐藏
- 无独立集成/冒烟测试 → 这两步合并到「监工判收」
- transcript 路径为空 → 无法展示对话记录，只能显示结果

## API 接口设计（后端新增）

### GET /api/tasks
返回所有任务概览（按状态分组）

```json
{
  "inbox": [{"task_id": "T-xxx", "prompt_preview": "前50字...", "created_at": "..."}],
  "running": [...],
  "done": [...],
  "needs_human": [...],
  "blocked": [...]
}
```

### GET /api/task/:task_id
返回单个任务详情 + 所有轮次

```json
{
  "task_id": "T-console-empty-hint",
  "prompt": "完整 prompt 文本",
  "checks": [{...}],
  "oracle_class": "A",
  "class_reason": "no rule matched",
  "status": "done",
  "attempts": [
    {
      "attempt_no": 1,
      "model": "haiku",
      "cost_usd": 0.12,
      "tokens_in": 5000,
      "tokens_out": 800,
      "wall_clock_s": 45,
      "resolution": "fail",
      "verdicts": [
        {"role": "regression", "verdict": "fail", "claims": [{...}]}
      ]
    },
    ...
    {
      "attempt_no": 9,
      "model": "sonnet",
      "resolution": "merged",
      "commit": "9f8aa24d13b8",
      ...
    }
  ],
  "total_cost_usd": 2.91,
  "total_time_s": 3200
}
```

### GET /api/stats
全局统计

```json
{
  "total_tasks": 10,
  "total_cost_usd": 29.12,
  "merged_count": 3,
  "escalated_count": 5,
  "avg_cost_per_task": 2.91
}
```

## 前端页面结构

```
/app                    # 首页：任务列表（卡片视图，按状态分组）
/app/task/:id          # 详情页：单任务时间线 + 轮次详情
/app/stats             # 统计页：费用/成功率图表
```

## UI 设计要点
- 时间线用垂直步骤条（类似 Ant Design Steps）
- 状态用颜色区分：inbox=灰、running=蓝+动画、done=绿、escalated=橙、blocked=红
- 费用醒目显示（大字号 + 美元符号）
- 监工判词折叠显示（默认收起，点击展开 JSON）
- 轮次卡片：attempt_no 大号显示，model 小标签，cost/tokens 淡色副文本
