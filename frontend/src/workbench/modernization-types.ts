/** Legacy modernization (信创化改造) view types — matches the shapes returned by
 *  every GET/POST on /api/v2/modernization (factory/control/modernization_routes.py,
 *  backed by factory/control/legacy_modernization.py). Deliberately not shared with
 *  maintenance-types.ts: same coincidental status vocabulary, different plugin,
 *  different SOURCE_TYPE — the two run in independent execution ports and should
 *  not be coupled just because today's shapes overlap. */

export type ModernizationStatus =
  | 'received'
  | 'running'
  | 'waiting'
  | 'cancelling'
  | 'delivered'
  | 'failed'
  | 'cancelled'

// --- Dimensions (the six axes a 信创 target expands into) ---

export type ModernizationDimension =
  | 'database'
  | 'os_cpu'
  | 'jdk_framework'
  | 'browser'
  | 'ca_signature'
  | 'external_component'

export const DIMENSIONS: ModernizationDimension[] = [
  'database', 'os_cpu', 'jdk_framework', 'browser', 'ca_signature', 'external_component',
]

const DIMENSION_LABELS: Record<ModernizationDimension, string> = {
  database: '数据库/版本',
  os_cpu: 'OS/CPU',
  jdk_framework: 'JDK/框架',
  browser: '浏览器',
  ca_signature: 'CA/签章',
  external_component: '外部组件',
}

/** 未知维度原样显示，好过显示一个编出来的名字。 */
export function dimensionLabel(dimension: string): string {
  return DIMENSION_LABELS[dimension as ModernizationDimension] ?? dimension
}

/** 目标登记的确认状态：candidate=评估建议，active=客户已确认。 */
export interface ModernizationDimensionEntry {
  dimension: ModernizationDimension
  label: string
  status: string
  content: string | null
  confirmed: boolean
  paths: string[]
  commit_sha: string | null
}

// --- Code location / disposal plan ---

export interface ModernizationLocateHit {
  path: string
  line: number | null
  end_line?: number | null
  name?: string | null
  kind?: string | null
  snippet?: string | null
  resolution?: string | null
}

/** 哪一层应答的定位请求。三个值含义不同，界面上不能混成一句「代码索引」：
 *  - `code_intel` 共享多语言层，读工作区，Java/C#/Go/Python 都能答
 *  - `codegraph-legacy` 仓库内的轻量索引，只解析 Python 与 JS/TS，只读已提交的 commit
 *  - `none` 没有可用索引
 *  回退层的空结果不代表代码里没有，所以它要单独说清楚覆盖范围。 */
export type ModernizationLocateSource = 'none' | 'code_intel' | 'codegraph-legacy'

export interface ModernizationLocateResult {
  query?: string
  source: ModernizationLocateSource
  fresh: boolean
  commit_sha: string | null
  results: ModernizationLocateHit[]
  /** 结果被上限截断；不是「只有这些」 */
  truncated?: boolean
  /** 工作区相对已索引版本有未提交改动 */
  dirty?: boolean | null
  /** 这次索引覆盖到的语言 */
  languages_indexed?: string[]
  /** 回退层才有：它一共只解析这几种语言 */
  covers?: string[]
  note?: string
}

/** 一句话说清这条定位是谁答的、有多新。绝不把回退层说成「代码索引」。 */
export function locateSourceLabel(location: ModernizationLocateResult): string {
  if (location.source === 'code_intel') {
    const freshness = location.fresh ? '最新' : '工作区已变动，可能过期'
    return `（共享代码索引 · ${freshness}${location.truncated ? ' · 结果被截断' : ''}）`
  }
  if (location.source === 'codegraph-legacy') {
    const covers = (location.covers ?? []).join('/') || 'Python、JS/TS'
    return `（仓库内轻量索引，只解析 ${covers} 的已提交版本；其它语言的空结果不代表代码里没有）`
  }
  return '（未建立代码索引）'
}

export interface ModernizationDisposalEntry {
  dimension: ModernizationDimension
  label: string
  target_status: string
  target_content: string | null
  blocked: boolean
  blocked_reason: string | null
  locations: ModernizationLocateResult[]
}

export interface ModernizationCodeLocation {
  path: string
  line: number | null
  source: string
}

// --- Slice shape ---

export interface ModernizationBaseline {
  repository: string
  base_sha: string
  base_branch_label: string
}

export interface ModernizationAgreement {
  revision: string
  skill_version: string
}

export interface ModernizationCheck {
  name: string
  passed: boolean
  exit_code: number
  reused: boolean
  identity_fingerprint?: string | null
}

export interface ModernizationDelivery {
  commit: string
  checks: ModernizationCheck[]
  /** 一条条未验证条件，不是一个笼统的布尔值：哪条维度、哪个已知缺口都要看得见。 */
  unverified: string[]
  working_copy_base_sha: string | null
  repository: string
  capability_source?: string
  synthetic?: boolean
}

export interface ModernizationStep {
  /** 服务端给的是 SOP 步骤键（intake…receipt），不是显示名。 */
  step: string
  state: string
}

const STEP_LABELS: Record<string, string> = {
  intake: '接收登记',
  locate: '定位范围',
  modify: '修改实现',
  verify: '验证检查',
  deliver: '交付补丁',
  receipt: '出具回执',
}

const STEP_STATE_LABELS: Record<string, string> = {
  done: '已完成',
  current: '进行中',
  pending: '未开始',
  blocked: '等待人工',
  stopped: '已停止',
}

/** 未知键原样显示，好过显示一个编出来的名字。 */
export function stepLabel(step: string): string {
  return STEP_LABELS[step] ?? step
}

export function stepStateLabel(state: string): string {
  return STEP_STATE_LABELS[state] ?? state
}

/** 现在停在哪一步：正在做的那步，否则最后一步做完的那步。 */
export function currentStep(steps: ModernizationStep[]): ModernizationStep | null {
  const active = steps.find(s => s.state !== 'done' && s.state !== 'pending')
  if (active) return active
  const done = [...steps].reverse().find(s => s.state === 'done')
  return done ?? steps[0] ?? null
}

export interface BlockingReason {
  kind: string
  message: string
  event?: { execution_id: string; sequence: number } | null
}

/** 已知阻塞原因的中文标题。键是服务端的事件种类，原文保留在事件日志里。 */
const BLOCKING_TITLES: Record<string, string> = {
  'run.failed': '执行失败，停下等人处理',
  'run.blocked': '执行被拦下',
  'run.recovered': '服务重启后接回，等待确认',
  'clarification.requested': '需要补充信息才能继续',
  'budget.exhausted': '预算用完，已停止',
  'approval.requested': '计划已就绪，等待人工批准后才会开始改动',
  'requirement_analysis.interrupted': '需求梳理被中断',
  unknown: '停下了，但没有留下可引用的原因',
}

/**
 * 面向使用者的中文标题；不认识的种类原样显示，不编一个。
 *
 * 原始种类码不会被这层替换掉：详情里仍然带着它，事件日志里也有对应的那条事件。
 */
export function blockingTitle(kind: string): string {
  return BLOCKING_TITLES[kind] ?? kind
}

/** 这个种类有没有中文说法——没有的话界面不该假装它有。 */
export function isKnownBlocking(kind: string): boolean {
  return kind in BLOCKING_TITLES
}

/** 停在 waiting 时该走哪个动作：approve 用的是同一个 blocking_reason.kind 字段
 *  服务端已经给出的信号，不是前端另猜一套规则；'awaiting_approval' 的执行状态
 *  正是通过 approval.requested 事件报告为阻塞原因的。 */
export function nextAction(status: ModernizationStatus, blockingReason: BlockingReason | null): 'approve' | 'resume' | 'clarify' | null {
  if (status !== 'waiting') return null
  if (blockingReason?.kind === 'approval.requested') return 'approve'
  // 停在提问上的运行要的是答案，不是「继续执行」——resume 只会把它推回同一个
  // 闸门。这里交给澄清面板，头部不再摆一个会误导的继续按钮。
  if (blockingReason?.kind === 'clarification.requested') return 'clarify'
  return 'resume'
}

export interface ModernizationReceipt {
  [key: string]: unknown
}

export interface ModernizationSliceView {
  /** 模型在动手前提出的、等待人工回答的问题。回答走 `.../clarify`，
   *  不要用 resume 顶替：停在提问上的运行需要的是答案。 */
  pending_questions?: string[]
  schema_version: number
  slice_id: string
  revision: number
  predecessor_id?: string | null
  successor_id?: string | null
  status: ModernizationStatus
  project_id: string
  dimension: ModernizationDimension
  scope_paths: string[]
  code_locations: ModernizationCodeLocation[]
  baseline: ModernizationBaseline
  agreement: ModernizationAgreement
  expected_behaviour: string
  delivery_goal: string
  delivery_tier: string
  known_gaps: string[]
  synthetic?: boolean
  execution_id?: string | null
  cost_usd?: number | null
  blocking_reason: BlockingReason | null
  steps: ModernizationStep[]
  delivery: ModernizationDelivery | null
  receipts: ModernizationReceipt[]
  created_at: string
}

/** The method version a new slice will be bound to, as the server reads it from the pack. */
export interface ModernizationAgreementInfo {
  revision: string
  skill_version: string
  pack: { id: string; name: string; version: number; validation_status: string }
}

export interface ModernizationEvent {
  sequence: number
  kind: string
  payload: unknown
  at: string
}

export interface ExportData {
  receipt: unknown
  text: string
  artifacts: Array<{ name: string; size: number }>
}

// --- Status display helpers ---

const STATUS_LABELS: Record<ModernizationStatus, string> = {
  received: '已接收',
  running: '执行中',
  waiting: '等待中',
  cancelling: '取消中',
  delivered: '已交付',
  failed: '失败',
  cancelled: '已取消',
}

export function modernizationStatusLabel(status: ModernizationStatus): string {
  return STATUS_LABELS[status] ?? status
}

type StatusTone = 'success' | 'danger' | 'warning' | 'active' | 'neutral'

const STATUS_TONES: Record<ModernizationStatus, StatusTone> = {
  received: 'neutral',
  running: 'active',
  waiting: 'warning',
  cancelling: 'warning',
  delivered: 'success',
  failed: 'danger',
  cancelled: 'danger',
}

export function modernizationStatusTone(status: ModernizationStatus): StatusTone {
  return STATUS_TONES[status] ?? 'neutral'
}

// --- Health evidence labels (same evidence rule as maintenance's checks) ---

export type HealthEvidence = 'current' | 'stale' | 'unchecked'

export function checkHealthEvidence(check: ModernizationCheck): HealthEvidence {
  if (!check.identity_fingerprint) return 'unchecked'
  return check.reused ? 'stale' : 'current'
}

const HEALTH_LABELS: Record<HealthEvidence, string> = {
  current: '当前',
  stale: '过期',
  unchecked: '未检查',
}

export function healthEvidenceLabel(evidence: HealthEvidence): string {
  return HEALTH_LABELS[evidence]
}

// --- Can-act helpers ---

export function canCancel(status: ModernizationStatus): boolean {
  return status === 'received' || status === 'running' || status === 'waiting'
}

// --- 业务插件可用性 ---
// 服务端是唯一裁决方：这里只决定界面提供什么入口，藏掉按钮从来不等于停用。

export type PluginState = 'enabled' | 'draining' | 'disabled'

export interface PluginAvailabilityView {
  id: string
  name: string
  version: number
  skill_pack_id: string
  contract: { host: number; min: number; max: number; compatible: boolean }
  entry_points: Record<string, string>
  host_capabilities: string[]
  ui_keys: string[]
  executable: boolean
  state: PluginState
  revision: number
  updated_at: string | null
  actor: string
  can_create: boolean
  can_continue: boolean
}

/** 为什么现在不能新建（登记目标/确认维度/新建切片/提交修订）。返回 null 表示可以。 */
export function pluginCreateBlockedReason(
  availability: PluginAvailabilityView | null,
): string | null {
  if (!availability || availability.can_create) return null
  if (availability.state === 'draining') {
    return `${availability.name}正在排空，暂时不接受新任务；已有切片仍可查询、导出与取消。`
  }
  return `${availability.name}已停用，暂时不能新建任务；历史切片与交付物仍然可以查看和下载。`
}
