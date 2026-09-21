/** Maintenance task view — matches the frozen contract in FUNCTION-FIRST-WIRING.md.
 *  This is the shape returned by every GET/POST on /api/v2/maintenance/tasks. */

export type MaintenanceStatus =
  | 'received'
  | 'running'
  | 'waiting'
  | 'cancelling'
  | 'delivered'
  | 'failed'
  | 'cancelled'

export interface MaintenanceIssue {
  source: string
  external_id: string
  version: string
  title: string
  body: string
}

export interface MaintenanceBaseline {
  repository: string
  base_sha: string
  base_branch_label: string
}

export interface MaintenanceAgreement {
  revision: number
  skill_version: number
}

export interface MaintenanceCheck {
  name: string
  passed: boolean
  exit_code: number
  reused: boolean
  identity_fingerprint?: string | null
}

export interface MaintenanceDelivery {
  commit: string
  checks: MaintenanceCheck[]
  unverified: boolean
  working_copy_base_sha: string
  repository: string
  capability_source?: string
  synthetic?: boolean
}

export interface MaintenanceStep {
  /** 服务端给的是 SOP 步骤键（intake…receipt），不是显示名。 */
  step: string
  state: string
}

const STEP_LABELS: Record<string, string> = {
  intake: '接收登记',
  triage: '定位分诊',
  reproduce: '复现问题',
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
export function currentStep(steps: MaintenanceStep[]): MaintenanceStep | null {
  const active = steps.find(s => s.state !== 'done' && s.state !== 'pending')
  if (active) return active
  const done = [...steps].reverse().find(s => s.state === 'done')
  return done ?? steps[0] ?? null
}

export interface BlockingReason {
  kind: string
  message: string
  event?: string
}

export interface MaintenanceReceipt {
  [key: string]: unknown
}

export interface MaintenanceTaskView {
  schema_version: number
  task_id: string
  revision: number
  predecessor_id?: string | null
  successor_id?: string | null
  status: MaintenanceStatus
  issue: MaintenanceIssue
  issue_digest: string
  project_id: string
  baseline: MaintenanceBaseline
  agreement?: MaintenanceAgreement | null
  expected_behaviour: string
  delivery_goal: string
  delivery_tier?: string | null
  synthetic?: boolean
  execution_id?: string | null
  cost_usd?: number | null
  blocking_reason: BlockingReason | null
  steps: MaintenanceStep[]
  delivery: MaintenanceDelivery | null
  receipts: MaintenanceReceipt[]
  supplements?: MaintenanceSupplement[]
  /** 服务端判断此刻「继续执行」是否会被接受；缺省时退回按状态判断。 */
  resumable?: boolean
  /** 停在计划批准节点时，待批准的计划本身。 */
  pending_plan?: MaintenancePendingPlan | null
  created_at: string
}

/** The method version a new task binds to, as the server reads it from the pack. */
export interface MaintenanceAgreementInfo {
  revision: string
  skill_version: string
  pack: { id: string; name: string; version: number; validation_status: string }
}

/** 等待人工批准的执行计划，连同它会改哪些文件、跑哪些检查。 */
export interface MaintenancePendingPlan {
  revision: number
  title: string
  summary: string
  questions: string[]
  tasks: { id: string; title: string; paths: string[]; checks: string[]; risk: string }[]
}

/** A follow-up the user added, as the durable events report it. */
export interface MaintenanceSupplement {
  id: string
  content: string
  created_at: string
  applied: boolean
  expired: boolean
  state: SupplementStatus
}

export interface MaintenanceEvent {
  sequence: number
  kind: string
  payload: unknown
  at: string
}

export interface FollowUpReceipt {
  recorded: boolean
  applied?: boolean
  /** What the server read back from the events, not what the click hoped for. */
  state?: SupplementStatus
  supplements?: MaintenanceSupplement[]
  [key: string]: unknown
}

export interface ExportData {
  receipt: unknown
  text: string
  artifacts: Array<{ name: string; size: number }>
}

// --- Status display helpers ---

const STATUS_LABELS: Record<MaintenanceStatus, string> = {
  received: '已接收',
  running: '执行中',
  waiting: '等待中',
  cancelling: '取消中',
  delivered: '已交付',
  failed: '失败',
  cancelled: '已取消',
}

export function maintenanceStatusLabel(status: MaintenanceStatus): string {
  return STATUS_LABELS[status] ?? status
}

type StatusTone = 'success' | 'danger' | 'warning' | 'active' | 'neutral'

const STATUS_TONES: Record<MaintenanceStatus, StatusTone> = {
  received: 'neutral',
  running: 'active',
  waiting: 'warning',
  cancelling: 'warning',
  delivered: 'success',
  failed: 'danger',
  cancelled: 'danger',
}

export function maintenanceStatusTone(status: MaintenanceStatus): StatusTone {
  return STATUS_TONES[status] ?? 'neutral'
}

// --- Health evidence labels (F3) ---

export type HealthEvidence = 'current' | 'stale' | 'unchecked'

export function checkHealthEvidence(check: MaintenanceCheck): HealthEvidence {
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

// --- Supplement (follow-up) labels (F3) ---

export type SupplementStatus = 'recorded' | 'pending' | 'applied' | 'expired'

export function supplementStatus(receipt: FollowUpReceipt): SupplementStatus {
  // The server says which of these the supplement actually is. Only a receipt
  // that carries no state at all falls back to "recorded", which claims the
  // least: it was stored, and nothing is claimed about it being used.
  if (receipt.state) return receipt.state
  if (receipt.applied) return 'applied'
  return receipt.recorded ? 'recorded' : 'pending'
}

const SUPPLEMENT_LABELS: Record<SupplementStatus, string> = {
  recorded: '已记录',
  pending: '待应用',
  applied: '已应用',
  expired: '已过期未应用',
}

export function supplementStatusLabel(status: SupplementStatus): string {
  return SUPPLEMENT_LABELS[status]
}

// --- Can-act helpers ---

export function canCancel(status: MaintenanceStatus): boolean {
  return status === 'received' || status === 'running' || status === 'waiting'
}

export function canResume(status: MaintenanceStatus, resumable?: boolean): boolean {
  // 服务端说得更准：停在没有计划的人工节点时，「继续执行」一定会被拒绝。
  if (resumable === false) return false
  return status === 'waiting'
}
