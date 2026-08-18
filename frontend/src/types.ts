// API 响应类型定义。形状来源：.hermes/plans/factory-frontend/contracts/api-shape.md
// 并按真实后端响应做了两处校准（见 Claim 注释）。

export type Resolution =
  | 'merged' | 'reworked' | 'escalated' | 'blocked' | 'not_dispatched';

export type QueueState =
  | 'inbox' | 'running' | 'done' | 'needs_human' | 'blocked';

export type VerdictRole = 'scope' | 'regression' | 'risk' | 'beacon';

export interface Claim {
  check: string;
  command?: string;
  expected?: string;
  /** 契约文档写的是 actual，真实后端返回的是 got。两个都可能出现，渲染时取存在的那个。 */
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

export interface TaskCheck {
  name: string;
  command: string;
}

export interface TaskDetail {
  task_id: string;
  state: QueueState;
  prompt: string;
  checks: TaskCheck[];
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
  // 队列读不到时后端把**每个 bucket 的值**置成 null（不是把整个对象置 null，
  // 也不是填 0）。实测 factory.api.global_stats 走 QueueUnreadable 分支时返回：
  //   {"inbox": null, "running": null, "done": null, "needs_human": null, "blocked": null}
  // 所以值类型必须是 number | null，前端把 null 渲染成 —。
  // 若写成 Record<QueueState, number>，TS 会把 by_state.done 当成必然是 number，
  // null 就会被当 0 显示，谎报「队列是空的」。
  by_state: Record<QueueState, number | null> | null;
  avg_cost_per_task_usd: number;
  avg_attempts_per_task: number;
}
