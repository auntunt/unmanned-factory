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

export interface PermissionEvent {
  tool: string;
  target: string;
  decision: 'allow' | 'deny' | 'escalate';
  rule: string;
  reason: string;
  tokens: number;
  cost_usd: number;
}

/** 人工验收结论。空串 = 还没人验过（≠ 不通过）。 */
export type HumanVerdict = '' | 'pass' | 'fail';

export interface Attempt {
  attempt_no: number;
  model: string;
  harness: string;
  harness_version: string;
  resolution: string;
  commit: string | null;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  wall_clock_s: number;
  created_at: string;
  verdicts: Verdict[];
  permission_events: PermissionEvent[];

  /** 定案理由（CLI `resolve --why` 和 Web 定案框写同一列）。老库里是空串。 */
  resolution_note?: string;

  // ---------- 人工验收 ----------
  // resolution 问「监工那条红准不准」，human_verdict 问「活干得好不好」。
  // 两个正交：一轮可以 merged 同时验收 fail（检查全绿但人不认 = 判据写窄了）。
  human_verdict?: HumanVerdict;
  human_note?: string;
  /** ISO 时刻，没验收过是 null。 */
  human_at?: string | null;

  // 现场正文不在详情里（一份 150KB，三轮半兆）。这两个布尔只说「有没有」，
  // 正文按需走 GET /api/task/<id>/attempt/<no>/transcript。
  // 可选是因为老后端不返回这俩字段，undefined 要按「不知道」处理而不是 false。
  has_transcript?: boolean;
  has_diff?: boolean;
}

/** 一轮的执行现场正文。GET /api/task/<id>/attempt/<no>/<transcript|diff> */
export interface AttemptArtifact {
  task_id: string;
  attempt_no: number;
  kind: 'transcript' | 'diff';
  /** 归档路径。null = 库里就没记（老 attempt / 非 claude harness）。 */
  path: string | null;
  /** 正文原样，未解析。空串合法（见 note）。 */
  body: string;
  bytes: number;
  /**
   * 后端为什么给不出完整正文。空串 = 正文完整。
   * **非空必须显示** —— 「文件随 /tmp 丢了」和「这一轮没产出」在界面上
   * 都是一片空白，但一个是数据事故一个是正常状态，不能让人自己猜。
   */
  note: string;
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
  // null = 队列不可读（队列侧挂了，审计侧的数仍可用）
  // 若写成 Record<QueueState, number>，TS 会把 by_state.done 当成必然是 number，
  // null 就会被当 0 显示，谎报「队列是空的」。
  by_state: Record<QueueState, number | null> | null;
  avg_cost_per_task_usd: number;
  avg_attempts_per_task: number;
  human_review: {
    passed: number;
    failed: number;
    pending: number;
  };
  score: {
    total: number | null;
    grade: string;
    sub: Record<string, number>;
    missing: string[];
    explain: string;
    weights: Record<string, number>;
  };
}

// ---------------------------------------------------------------------------
// /api/analytics —— 总览页的聚合响应。后端：factory/api_analytics.py
// ---------------------------------------------------------------------------

/**
 * 一个带口径的指标。
 *
 * 为什么不直接给 number：同一个「自动化率」在不同分母下能差一倍。分母和
 * 口径文字跟值绑在同一个对象里，渲染时无法只显示百分比而漏掉口径 ——
 * 类型层面就把「裸百分比」这条路堵掉。
 */
export interface Metric {
  /** null = 分母为 0，算不出来。渲染成 —，不是 0%。 */
  value: number | null
  /** 分母。 */
  of: number
  /** 分子。 */
  hit: number
  /** 口径的一句话说明，必须显示在值旁边。 */
  basis: string
  label: string
  /** 有 unit 表示这是个绝对量（如「轮」），不加 %。 */
  unit?: string
}

export type StageSource = 'real' | 'derived'

export interface Stage {
  key: string
  name: string
  /** real = 队列里数出来的事实；derived = 从 attempt 推的瞬时态。 */
  source: StageSource
  /** derived 节点专有：这个数是怎么推出来的。 */
  derived_from?: string
  desc: string
  terminal?: boolean
  /** null = 队列读不到。渲染 —。 */
  count: number | null
}

export interface TrendPoint {
  date: string
  runs: number
  merged: number
  cost_usd: number
}

export interface ModelCost {
  model: string
  runs: number
  cost_usd: number
  tokens_in: number
  tokens_out: number
  share: number | null
}

export interface GateRow {
  gate_id: string
  role: string
  gate: string
  fired: number
  true_positives: number
  false_positives: number
  unadjudicated: number
  /** null = 没定案过，命中率「不知道」。不是 0。 */
  precision: number | null
  verdict: string
}

export interface StuckTask {
  task_id: string
  state: string
  preview: string
  stuck_days: number | null
  attempts: number
  cost_usd: number
}

export interface Analytics {
  generated_at: string
  window_days: number
  core: Record<string, Metric>
  stages: Stage[]
  trend: TrendPoint[]
  cost: {
    total_usd: number
    per_task_usd: number | null
    tokens_in: number
    tokens_out: number
    by_model: ModelCost[]
    wasted_usd: number
  }
  gates: {
    rows: GateRow[]
    never_fired: number
    roles: {
      role: string
      fired: number
      passed: number
      true_positives: number
      false_positives: number
      unadjudicated: number
      precision: number | null
      verdict: string
    }[]
    /** 这张表能不能用来做裁剪决策，以及为什么。必须显示。 */
    caveat: {
      unadjudicated: number
      adjudicated: number
      reliable: boolean
      text: string
    }
  }
  stuck: StuckTask[]
  resolutions: Record<string, number>
  /** true = 确实一轮都没跑过（区别于 degraded 的「读不到」）。 */
  empty: boolean
  /** 非空 = 有部分数据源挂了，逐条点名。 */
  degraded: string[]
}
