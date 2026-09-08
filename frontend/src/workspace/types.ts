export type RunStatus =
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
  id: string | number
  project_id: string | number
  request: string
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

export function runId(run: Pick<Run, 'id'>): string {
  return String(run.id)
}
