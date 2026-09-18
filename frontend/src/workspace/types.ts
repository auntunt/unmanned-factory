import type { SpecDraft, FidelityTarget, RecommendedSkill } from '../workbench/RequirementConfirmation'
export type RunStatus =
  | 'requirement_analysis'
  | 'awaiting_spec_confirmation'
  | 'inspection_completed'
  | 'inspection_failed'
  | 'received'
  | 'planning'
  | 'needs_clarification'
  | 'awaiting_approval'
  | 'queued'
  | 'running'
  | 'verifying'
  | 'ready_for_review'
  | 'publishing'
  | 'published'
  | 'needs_human'
  | 'failed'
  | 'discarded'
  | 'cancelled'

export type Complexity = 'small' | 'medium' | 'large'
export type Risk = 'low' | 'medium' | 'high'
export type TaskStatus = 'pending' | 'queued' | 'running' | 'verified' | 'completed' | 'failed' | 'blocked' | 'cancelled'

export interface User {
  id: string | number
  username: string
  role?: 'admin' | 'member'
  active?: boolean
}

export interface Project {
  auto_spec_confirm?: boolean
  requirement_analysis_budget_usd?: number | null
  spec_tree_enabled?: boolean
  id: string | number
  name: string
  repository: string
  workspace: string
  base_branch: string
  checks: Record<string, string[]>
  auto_issues: boolean
  auto_publish: boolean
}

export interface ProviderInfo {
  id: string
  installed: boolean
  detail?: string | null
}

export interface ProviderProfile {
  provider?: string
  model?: string
}

export interface ProvidersResponse {
  providers: ProviderInfo[]
  profiles: Record<string, ProviderProfile>
}

export interface PlanTask {
  id: string
  title: string
  prompt: string
  acceptance: string[]
  paths: string[]
  checks: string[]
  depends_on: string[]
  complexity: Complexity
  risk: Risk
  status?: TaskStatus
}

export interface Plan {
  title: string
  summary: string
  questions: string[]
  tasks: PlanTask[]
}

export interface Triage {
  decision: 'auto_execute' | 'needs_clarification' | 'human_approval'
  reasons: string[]
  questions: string[]
  risk: Risk
}

export interface Artifacts {
  branch?: string | null
  base_sha?: string | null
  commit?: string | null
  pr_url?: string | null
  checks?: unknown
  tasks?: unknown
  worktree?: string | null
  [key: string]: unknown
}

export interface Run {
  followups?: { id: string; content: string; created_at: string; applied: boolean; expired?: boolean }[]
  root_request?: string
  error?: string | null
  retry_run_id?: string
  resume_count?: number
  spec_draft?: SpecDraft
  recommended_skills?: RecommendedSkill[]
  requirement_skill_catalog?: { id: string; name: string; version: number }[]
  fidelity_target?: FidelityTarget | null
  spec_confirmation?: { actor: string; automatic: boolean; at: string }

  inspection_superseded_by?: string
  execution_mode?: 'continuous' | 'dag'
  id: string | number
  project_id: string | number | null
  request: string
  source?: Record<string, unknown>
  status: RunStatus
  revision: number
  plan?: Plan | null
  triage?: Triage | null
  tasks?: unknown
  artifacts?: Artifacts | null
  created_at: string
  updated_at: string
}

export interface ConversationMessage {
  followup?: boolean
  applied?: boolean
  pending_id?: string
  id: string | number
  role: string
  content: string
  event_ids?: Array<string | number>
  at: string
}

export interface AuditEvent {
  id: number
  version: number
  run_id: string | number
  task_id?: string | null
  type: string
  payload: unknown
  at: string
}

// --- Capability source types (N6) ---

export type CapabilitySourceStatus = 'available' | 'no_record'
export type LoadedOrigin = 'project_module' | 'session_skill'

export interface LoadedItem {
  name: string
  id: string
  origin: LoadedOrigin
}

export interface InvokedItem {
  name: string
  count: number
  first_at: string
  last_at: string
}

export interface CapabilitySources {
  loaded: { status: CapabilitySourceStatus; items: LoadedItem[] }
  invoked: { status: CapabilitySourceStatus; items: InvokedItem[] }
}

export interface ServiceUrl {
  name: string
  url: string
}

export function runId(run: Pick<Run, 'id'>): string {
  return String(run.id)
}
