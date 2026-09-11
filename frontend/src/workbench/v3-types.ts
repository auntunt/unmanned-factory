import type { Artifacts, Project, Run } from '../workspace/types'

export interface EngineeringStageItem {
  id: string
  title: string
  project_name?: string
  status: string
  updated_at: string
  href: string
  detail?: string
}

export interface EngineeringStage {
  id: 'intake' | 'plan' | 'build' | 'verify' | 'deliver' | 'reuse' | string
  label: string
  description: string
  count: number
  unit: string
  items: EngineeringStageItem[]
}

export interface EngineeringSummary {
  stages: EngineeringStage[]
  verified_runs: number
  published_runs: number
  distilled_runs: number
  reused_runs: number
  draft_capabilities: number
  ready_capabilities: number
}

export interface OverviewData {
  snapshot_at?: string
  run_snapshots?: Run[]
  project_id?: string | null
  project_summaries?: ProjectSummary[]
  projects: number
  runs: number
  active_runs: number
  attention_runs: number
  delivered_runs: number
  known_cost_usd: number
  unknown_cost_runs: number
  recent_events: Array<{ id: number; run_id: string; task_id?: string | null; type: string; payload: unknown; at: string; project_name?: string; run_title?: string }>
  attention?: Array<{ run_snapshot?: Run; id: string; project_id?: string; project_name?: string; title?: string; status: string; updated_at: string; reason?: string; questions?: string[]; billing_incomplete?: unknown }>
  model_usage: Array<{
    profile: string
    provider?: string
    model?: string
    calls: number
    known_cost_usd: number
    unknown_cost_calls: number
    input_tokens: number
    output_tokens: number
    cached_input_tokens: number
    cache_creation_input_tokens: number
    token_usage_calls: number
    cache_usage_calls: number
  }>
  activity: Array<{ date: string; runs: number; delivered: number }>
  capabilities: number
  engineering?: EngineeringSummary
}

export interface ProjectSummary {
  next_run?: Run | null
  id: string
  name: string
  repository: string
  budget_usd?: number
  run_count: number
  active_runs: number
  attention_runs: number
  engineering: EngineeringSummary
}

export interface Capability {
  id: string
  revision: number
  name: string
  description: string
  category: string
  instructions: string
  input_description: string
  output_description: string
  acceptance: string[]
  status: 'draft' | 'ready' | string
  created_at: string
  updated_at: string
  source_run_id?: string
}

export interface CapabilityDetail extends Capability { versions: Capability[] }
export interface CapabilityBinding { capability_id: string; revision: number; name: string; status: string }
export interface Policy {
  revision: number
  mode: 'supervised' | 'autonomous'
  max_risk: 'low' | 'medium' | 'high'
  max_attempts: number
  auto_escalate: boolean
  resume_on_restart: boolean
}

export type V3Project = Project & { revision?: number; budget_usd?: number }
export type V3Run = Run & { source?: Record<string, unknown>; capability?: Capability & { source?: Record<string, unknown> }; capabilities?: Capability[]; planner_usage?: Record<string, unknown>; previous_run_id?: string }
export type V3Artifacts = Artifacts & { known_cost_usd?: number; observed_cost_usd?: number | null; billing_incomplete?: unknown; attempts?: unknown }
