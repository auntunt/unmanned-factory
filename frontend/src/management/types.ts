// 类型对应 factory/control/org_governance.py 的返回体；只覆盖 /api/v5/* 用到的字段。

export interface Workspaces {
  management: boolean
  admin: boolean
  scopes: { unit_id: string; name: string; path: string }[]
  execution_note: string
}

export interface ScopeUnit { id: string; name: string; path: string; kind_label: string }
export interface ScopeGrant { unit_id: string; path: string }

export interface OverviewCounts {
  in_progress: number
  pending: number
  ready: number
  published: number
  failed: number
}

export interface OverviewProject {
  id: string
  name: string
  unit_id: string | null
  unit_path: string | null
  counts: OverviewCounts
  initiators: string[]
  last_activity_at: string | null
  run_count: number
}

export interface OverviewPending {
  run_id: string | number
  project_id: string
  project_name: string
  title: string
  status: string
  reason: string
  initiator: string | null
  updated_at: string | null
  can_act: boolean
  action_hint: string
}

export interface OverviewUsage {
  window_days: number
  runs_considered: number
  recorded: boolean
  known_cost_usd: number | null
  unknown_cost_calls: number
  calls: number
  cost_source: string
  tokens: {
    month: string
    settled_tokens: number
    unknown_calls: number
    reserved_calls: number
    source: string
    recorded: boolean
  }
}

export interface OverviewAuditRow {
  id: number
  actor: string
  action: string
  at: string
  data: Record<string, unknown>
}

export interface Overview {
  scope: {
    unit_id: string | null
    label: string
    admin: boolean
    units: ScopeUnit[]
    grants: ScopeGrant[]
  }
  window: { days: number; since: string; note: string }
  generated_at: string
  counts: OverviewCounts
  projects: OverviewProject[]
  pending: OverviewPending[]
  usage: OverviewUsage
  audit: OverviewAuditRow[]
  notes: { published: string; responsibility: string; execution: string }
  unassigned_projects?: { id: string; name: string }[]
}

export interface ProjectDetailRun {
  run_id: string | number
  title: string
  status: string
  initiator: string | null
  created_at: string
  updated_at: string
  known_cost_usd: number | null
  unknown_cost_calls: number
  can_act: boolean
}

export interface ProjectDetail {
  project: { id: string; name: string; unit_path: string | null }
  runs: ProjectDetailRun[]
  truncated: boolean
  generated_at: string
  note: string
}

// ---- /api/v5/org (admin) ----

export type UnitKind = 'company' | 'department' | 'group'

export interface OrgUnit {
  id: string
  name: string
  kind: UnitKind
  parent_id: string | null
  created_at: string
  kind_label: string
  path: string
  project_ids: string[]
}

export interface OrgProject { id: string; name: string; unit_id: string | null }

export interface OrgScope {
  user_id: number
  unit_id: string
  granted_by: string
  granted_at: string
  username: string
  path: string
}

export interface OrgUser { id: number; username: string; role: 'admin' | 'member'; active: boolean }

export interface OrgTree {
  units: OrgUnit[]
  projects: OrgProject[]
  unassigned_project_ids: string[]
  scopes: OrgScope[]
  users: OrgUser[]
}

export const KIND_LABEL: Record<UnitKind, string> = { company: '公司', department: '部门', group: '小组' }
