// 运维维护子系统的共享契约类型：maintenance-subsystem/1。
// 与 docs/maintenance-subsystem/CONTRACT.md 一一对应；页面与画布不持有业务状态，
// 这里的每个字段都由后端派生。改字段先改后端与 CONTRACT.md。

export const CONTRACT_VERSION = 'maintenance-subsystem/1'

export type SourceStatus = 'connected' | 'online' | 'offline' | 'unknown' | 'not_connected' | 'error'

export interface DataSource {
  status: SourceStatus
  detail?: string
}

export type AttentionKind = 'answer' | 'approval' | 'blocked'

export interface AttentionItem {
  task_id: string
  project_id: string
  project_name: string
  title: string
  kind: AttentionKind
  reason: string
  since: string | null
}

export interface ProjectRow {
  project_id: string
  name: string
  repository: string
  repo_state: RepoState
  repo_state_label: string
  running: number
  waiting: number
  delivered: number
  delivery_target: string
  service_status: 'not_connected' | string
  synthetic?: boolean
}

export interface MonitorEvent {
  task_id: string
  project_id: string
  execution_id: string
  sequence: number
  kind: string
  label: string
  at: string
}

export interface Overview {
  contract_version: string
  generated_at: string
  window: { kind: 'day'; start: string; end: string; timezone: string }
  scope: { project_ids: string[] }
  sources: {
    maintenance: DataSource
    executor: DataSource
    server_metrics: DataSource
    app_probes: DataSource
    alerts: DataSource
  }
  counts: {
    projects: number | null
    running: number | null
    attention: { total: number; answer: number; approval: number; blocked: number } | null
    delivered_in_window: number | null
  }
  attention: AttentionItem[]
  projects: ProjectRow[]
  events: MonitorEvent[]
  queue: { pending: number | null; running: number | null; executor: DataSource }
  availability: { state: string; [key: string]: unknown }
  partial_errors: { section: string; message: string }[]
}

export type NodeType = 'repo' | 'requirement' | 'task' | 'artifact' | 'target' | 'blocker'

export interface GraphNode {
  id: string
  type: NodeType
  label: string
  sublabel: string
  status: string
  task_id: string | null
  project_id: string
  synthetic?: boolean
}

export interface GraphEdge {
  from: string
  to: string
  kind: 'has' | 'creates' | 'produces' | 'targets' | 'blocked_by'
}

export interface Graph {
  generated_at: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  truncated: boolean
}

export type RepoState = 'pending' | 'analyzing' | 'needs_input' | 'ready' | 'failed'

export interface Finding {
  id: string
  label: string
  status: 'found' | 'verified' | 'missing' | 'failed'
  message: string
}

export interface SuggestedCheck {
  name: string
  argv: string[]
  evidence: string
  /** 命令能否在执行主机上启动（未运行）；false 时采纳后检查会失败。 */
  available?: boolean
}

export interface RepoView {
  project_id: string
  name: string
  repository: string
  workspace: string
  base_branch: string
  reused?: boolean
  state: RepoState
  state_label: string
  probe: {
    at: string | null
    head_sha: string | null
    branch: string | null
    remote: string | null
    access: { ok: boolean; message: string }
    stack: { name: string; evidence: string }[]
    suggested_checks: SuggestedCheck[]
    findings: Finding[]
  } | null
  needs: string[]
  checks_configured: string[]
  memory: { entries: number | null; confirmed: number | null; code_index: string }
  credential_ref: string | null
  synthetic?: boolean
  requirements?: Requirement[]
  tasks?: { task_id: string; title: string; status: string; created_at: string }[]
}

export type RequirementStatus = 'dispatched' | 'pending_dispatch' | 'dispatch_failed'

export interface Requirement {
  requirement_id: string
  project_id: string
  project_name?: string
  source: { kind: 'manual' | 'api' | 'cli'; name: string }
  external_id: string | null
  title: string
  content: string
  attachments: { name: string; ref: string }[]
  received_at: string
  status: RequirementStatus
  status_label: string
  task_id: string | null
  task_status: string | null
  dispatch_error: string | null
  synthetic?: boolean
}

export interface Receipt {
  requirement_id: string
  status: RequirementStatus
  duplicate: boolean
  task_id: string | null
  execution_id: string | null
  clarification: { state: 'analysis_pending' | 'questions' | 'not_needed'; questions: unknown[] }
  received_at: string
  source: { kind: 'manual' | 'api' | 'cli'; name: string }
  message: string
  synthetic?: boolean
}

export interface IntakeSource {
  id: string
  name: string
  project_ids: string[]
  auto_dispatch: boolean
  created_at: string
  revoked_at: string | null
  token_hint: string
  synthetic?: boolean
  /** 只在创建时出现一次。 */
  token?: string
}

export type TaskAction = 'answer' | 'approve' | 'supplement' | 'resume' | 'cancel' | 'feedback' | 'export'

export const REPO_STATE_LABEL: Record<RepoState, string> = {
  pending: '待分析', analyzing: '分析中', needs_input: '待补充', ready: '可开始维护', failed: '接入失败',
}

export const ATTENTION_LABEL: Record<AttentionKind, string> = {
  answer: '等待业务回答', approval: '等待批准', blocked: '执行受阻',
}
