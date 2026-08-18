## API 响应形状（T03/T04/T05 共用，前端按这个写类型）

以下是 T01 实现的三个端点的确切返回结构。前端 TypeScript 类型照这个定义。

### GET /api/tasks

```json
{
  "inbox": [
    {
      "task_id": "T-add-clear-input-btn-1",
      "prompt_preview": "给控制台输入框加一个清空按钮，点了把 textarea 清空…",
      "checks_count": 1,
      "mtime": "2026-08-18T05:26:49"
    }
  ],
  "running": [],
  "done": [
    {
      "task_id": "T-console-empty-hint",
      "prompt_preview": "factory/console_read.py 的 read_threads() 现在返回…",
      "checks_count": 1,
      "mtime": "2026-08-18T06:09:05",
      "resolution": "merged",
      "attempts_count": 9,
      "total_cost_usd": 2.909712
    }
  ],
  "needs_human": [],
  "blocked": []
}
```

要点：
- 五个桶恒定存在，没任务就是空数组（不要 null）
- `prompt_preview` 是前 60 字符 + `…`
- done / needs_human 桶里的条目多带 `resolution` / `attempts_count` / `total_cost_usd`
- inbox / running 桶里的条目没有这三个字段（还没跑过）
- `mtime` 是 ISO 8601 字符串

### GET /api/task/:task_id

```json
{
  "task_id": "T-console-empty-hint",
  "state": "done",
  "prompt": "完整 prompt 全文，含换行",
  "checks": [
    {"name": "pytest_console", "command": "uv run pytest tests/test_console.py -q"}
  ],
  "oracle_class": "A",
  "class_reason": "no rule matched -> default A",
  "total_cost_usd": 2.909712,
  "total_wall_clock_s": 355.8,
  "attempts": [
    {
      "attempt_no": 1,
      "model": "haiku",
      "harness": "claude_code",
      "harness_version": "2.1.234 (Claude Code)+bwrap",
      "resolution": "reworked",
      "commit": null,
      "cost_usd": 0.12,
      "tokens_in": 5000,
      "tokens_out": 800,
      "wall_clock_s": 45.2,
      "created_at": "2026-08-18T05:30:11",
      "verdicts": [
        {
          "role": "scope",
          "verdict": "pass",
          "claims": []
        },
        {
          "role": "regression",
          "verdict": "fail",
          "claims": [
            {
              "check": "pytest_console",
              "command": "uv run pytest tests/test_console.py -q",
              "expected": "exit_status ok",
              "actual": "exit 127: .venv/bin/python: not found"
            }
          ]
        }
      ]
    }
  ]
}
```

要点：
- `attempts` 按 `attempt_no` 升序
- `resolution` 取值：`merged` / `reworked` / `escalated` / `blocked` / `not_dispatched`
- `commit` 只有 merged 那轮非 null
- `wall_clock_s` 是秒（后端把 ms 除以 1000），不要让前端做单位换算
- `verdicts` 里 `role` 取值：`scope` / `regression` / `risk` / `beacon`
- `claims` 是数组，pass 时通常为空数组
- task_id 不存在 → HTTP 404 + `{"error": "task not found: T-xxx"}`

### GET /api/stats

```json
{
  "total_tasks": 4,
  "total_attempts": 10,
  "total_cost_usd": 29.1234,
  "by_resolution": {
    "merged": 1,
    "escalated": 3,
    "reworked": 6
  },
  "by_state": {
    "inbox": 0,
    "running": 0,
    "done": 1,
    "needs_human": 2,
    "blocked": 0
  },
  "avg_cost_per_task_usd": 7.28,
  "avg_attempts_per_task": 2.5
}
```

### 前端 TypeScript 类型（T03/T04/T05 直接用）

建议放在 `frontend/src/types.ts`：

```typescript
export type Resolution =
  | 'merged' | 'reworked' | 'escalated' | 'blocked' | 'not_dispatched';

export type QueueState =
  | 'inbox' | 'running' | 'done' | 'needs_human' | 'blocked';

export type VerdictRole = 'scope' | 'regression' | 'risk' | 'beacon';

export interface Claim {
  check: string;
  command?: string;
  expected?: string;
  // 实测校正：真实后端返回的字段名是 got，不是 actual。两个都声明为可选，
  // 渲染时用 actual ?? got，UI 标签统一显示 ACTUAL。
  actual?: string;
  got?: string;
}

export interface Verdict {
  role: VerdictRole;
  verdict: 'pass' | 'fail';
  claims: Claim[];
}

export interface Attempt {
  attempt_no: number;
  model: string;
  harness: string;
  harness_version: string;
  resolution: Resolution;
  commit: string | null;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  wall_clock_s: number;
  created_at: string;
  verdicts: Verdict[];
}

export interface TaskSummary {
  task_id: string;
  prompt_preview: string;
  checks_count: number;
  mtime: string;
  resolution?: Resolution;
  attempts_count?: number;
  total_cost_usd?: number;
}

export interface TaskDetail {
  task_id: string;
  state: QueueState;
  prompt: string;
  checks: { name: string; command: string }[];
  oracle_class: string;
  class_reason: string;
  total_cost_usd: number;
  total_wall_clock_s: number;
  attempts: Attempt[];
}

export type TaskBuckets = Record<QueueState, TaskSummary[]>;

export interface Stats {
  total_tasks: number;
  total_attempts: number;
  total_cost_usd: number;
  by_resolution: Record<string, number>;
  by_state: Record<QueueState, number>;
  avg_cost_per_task_usd: number;
  avg_attempts_per_task: number;
}
```
