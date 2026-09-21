/** Adaptation task view — matches the shape `AdaptationTasks._view` in
 *  `factory/control/api_adaptation.py` returns from every GET/POST on
 *  `/api/v2/adaptation/tasks`.
 *
 *  Kept as this page's own copy rather than importing from maintenance-types,
 *  the same discipline `api_adaptation.py` documents for its own backend
 *  module ("this module's own copy rather than imported from
 *  issue_maintenance"): an adaptation task is not a maintenance task wearing a
 *  different label, and this file must not silently start tracking whatever a
 *  future maintenance-only UI change does to its labels or thresholds. */

export type AdaptationStatus =
  | 'received'
  | 'running'
  | 'waiting'
  | 'cancelling'
  | 'delivered'
  | 'failed'
  | 'cancelled'

export interface AdaptationBaseline {
  repository: string
  base_sha: string
  base_branch_label: string
}

export interface AdaptationSourceApi {
  name: string
  version: string
  base_url: string
  auth: string
  environment: 'mock' | 'sandbox' | 'production'
}

export interface AdaptationTargetAdapter {
  name: string
  entry: string
}

export interface AdaptationContract {
  source_api: AdaptationSourceApi
  target_adapter: AdaptationTargetAdapter
  endpoints: unknown[]
  fields: Record<string, unknown>
}

export interface ContractDiff {
  source_api_version: { old: string; new: string }
  fields_added: string[]
  fields_removed: string[]
  fields_changed: string[]
}

export interface AffectedMapping {
  source_field: string
  target_field: string
  reason: string
}

/** One row of the field mapping matrix. A field is either mapped (has a
 *  target_field) or explicitly unmapped (has a stated impact) — there is no
 *  third, silently-dropped option; `api_adaptation.normalize` refuses it. */
export interface MappingEntry {
  source_field: string
  mapped: boolean
  target_field: string
  type: string
  unit: string
  enum: string[]
  null_semantics: string
  precision: string
  encoding: string
  timezone: string
  error_code: string
  transform: string
  impact: string
  notes: string
}

export interface AdaptationMessage {
  name: string
  method: string
  path: string
  idempotent: boolean
  retry_policy: string
  sample_payload: Record<string, unknown>
}

export interface AdaptationAgreement {
  revision: string
  skill_version: string
}

export interface AdaptationCheck {
  name: string
  passed: boolean
  exit_code: number
  reused: boolean
  identity_fingerprint?: string | null
}

/** The real business-call outcome, read out of a check's own captured stdout
 *  after a real call was made — never invented from an HTTP status code and
 *  never taken on the coding executor's word (`api_adaptation._business_call`).
 *  The three flags travel separately on purpose: a 200 or a mock success is
 *  never business_completed. */
export interface BusinessCall {
  correlation_id: string | null
  technical_success: boolean | null
  business_accepted: boolean | null
  business_completed: boolean | null
  mock: boolean
  endpoint: string | null
  sanitized_response: Record<string, unknown> | null
  check_name?: string | null
}

export interface AdaptationDelivery {
  commit: string
  checks: AdaptationCheck[]
  unverified: string[]
  working_copy_base_sha: string | null
  repository: string | null
  synthetic?: boolean
  worktree?: string | null
  business_call: BusinessCall | null
}

export interface AdaptationCounts {
  http_operations: number
  business_messages: number
  fields: number
}

export interface AdaptationReceiptRecord {
  revision: number
  at: string
  [key: string]: unknown
}

export interface AdaptationTaskView {
  schema_version: number
  task_id: string
  revision: number
  predecessor_id?: string | null
  successor_id?: string | null
  status: AdaptationStatus
  project_id: string
  baseline: AdaptationBaseline
  contract: AdaptationContract
  contract_digest: string
  contract_diff: ContractDiff | null
  affected_mappings: AffectedMapping[]
  mapping_matrix: MappingEntry[]
  unmapped_fields: MappingEntry[]
  counts: AdaptationCounts
  message: AdaptationMessage
  agreement: AdaptationAgreement
  expected_behaviour: string
  delivery_goal: string
  delivery_tier: string
  synthetic: boolean
  execution_id?: string | null
  cost_usd?: number | null
  delivery: AdaptationDelivery | null
  business_call: BusinessCall | null
  receipts: AdaptationReceiptRecord[]
  created_at: string
}

export interface AdaptationEvent {
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

const STATUS_LABELS: Record<AdaptationStatus, string> = {
  received: '已接收',
  running: '执行中',
  waiting: '等待中',
  cancelling: '取消中',
  delivered: '已交付',
  failed: '失败',
  cancelled: '已取消',
}

export function adaptationStatusLabel(status: AdaptationStatus): string {
  return STATUS_LABELS[status] ?? status
}

type StatusTone = 'success' | 'danger' | 'warning' | 'active' | 'neutral'

const STATUS_TONES: Record<AdaptationStatus, StatusTone> = {
  received: 'neutral',
  running: 'active',
  waiting: 'warning',
  cancelling: 'warning',
  delivered: 'success',
  failed: 'danger',
  cancelled: 'danger',
}

export function adaptationStatusTone(status: AdaptationStatus): StatusTone {
  return STATUS_TONES[status] ?? 'neutral'
}

export function canCancelAdaptation(status: AdaptationStatus): boolean {
  return status === 'received' || status === 'running' || status === 'waiting'
}

// --- Mapping matrix helpers ---
// Mapped and unmapped rows must never be shown mixed together (SHARED.md /
// REVIEW-9a61d62.md #3): the unmapped side carries a stated business impact
// that must stay visible on its own, not buried in a shared table.

export function mappedRows(view: Pick<AdaptationTaskView, 'mapping_matrix'>): MappingEntry[] {
  return view.mapping_matrix.filter(m => m.mapped)
}

export function unmappedRows(view: Pick<AdaptationTaskView, 'mapping_matrix' | 'unmapped_fields'>): MappingEntry[] {
  // Prefer the server's own `unmapped_fields` (exactly what normalize() computed);
  // fall back to filtering the matrix so a receipt-shaped object without that
  // field still renders correctly.
  if (view.unmapped_fields) return view.unmapped_fields
  return view.mapping_matrix.filter(m => !m.mapped)
}

// --- Business-call helpers ---
// technical_success / business_accepted / business_completed are three
// separate booleans (or null when never observed). None of them may ever be
// collapsed into, or read as a proxy for, another.

export type TriState = boolean | null | undefined

export function triLabel(value: TriState, whenTrue: string, whenFalse: string): string {
  if (value === true) return whenTrue
  if (value === false) return whenFalse
  return '未记录'
}

export function triTone(value: TriState): StatusTone {
  if (value === true) return 'success'
  if (value === false) return 'danger'
  return 'neutral'
}

export function technicalSuccessLabel(call: BusinessCall | null | undefined): string {
  return triLabel(call?.technical_success, '技术成功', '技术未成功')
}

export function businessAcceptedLabel(call: BusinessCall | null | undefined): string {
  return triLabel(call?.business_accepted, '业务已受理', '业务未受理')
}

export function businessCompletedLabel(call: BusinessCall | null | undefined): string {
  return triLabel(call?.business_completed, '业务已完成', '业务未完成')
}

/** Why `business_completed === false` must not read as a failure of the other
 *  two flags: this is usually a one-way report with no receipt channel to
 *  confirm final completion, mirroring `render_receipt` on the backend. */
export function businessCompletedCaveat(call: BusinessCall | null | undefined): string | null {
  if (!call || call.business_completed !== false) return null
  return '本期为单向上报，无回执渠道确认最终完成，不等同于业务受理或技术成功。'
}

/** The mock marker must always be visible and must never read as a real
 *  business completion — a mock success is not equivalent to production. */
export function mockLabel(call: BusinessCall | null | undefined): string {
  if (!call) return '未联调'
  return call.mock ? '本地模拟联调（非真实第三方）' : '真实第三方环境联调'
}

export function mockTone(call: BusinessCall | null | undefined): StatusTone {
  if (!call) return 'neutral'
  return call.mock ? 'warning' : 'success'
}

// --- Delivery tier ---

const DELIVERY_TIER_LABELS: Record<string, string> = {
  package: '交包待发布，未部署到任何环境',
  authorized_test_project: '已部署到明确授权的测试项目',
}

export function deliveryTierLabel(tier: string): string {
  return DELIVERY_TIER_LABELS[tier] ?? tier
}

// --- 业务插件可用性 ---
// Own copy of the same shape maintenance-types.ts declares (both read the
// same `PluginAvailability.view` on the server) — kept separate for the same
// reason the backend keeps its own ACTIONS table rather than importing
// issue_maintenance's: this page must not silently inherit a future
// maintenance-only change to what "blocked" means.

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

/** Why new work (create or revise) can't be started right now. Returns null
 *  when it can. The server is still the real enforcement point — this only
 *  decides whether to show a button that would just come back refused. */
export function pluginCreateBlockedReason(
  availability: PluginAvailabilityView | null,
): string | null {
  if (!availability || availability.can_create) return null
  if (availability.state === 'draining') {
    return `${availability.name}正在排空，暂时不接受新任务或新修订；已有任务仍可查询、导出与取消。`
  }
  return `${availability.name}已停用，暂时不能新建或修订任务；历史任务与交付物仍然可以查看和下载。`
}
