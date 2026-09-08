/** Types returned by the authenticated runtime configuration API. */

export const RUNTIME_ROLES = ['planner', 'cheap', 'standard', 'strong'] as const
export type RuntimeRole = (typeof RUNTIME_ROLES)[number]

export const RUNTIME_PROVIDERS = ['claude', 'codex', 'dsh'] as const
export type RuntimeProvider = (typeof RUNTIME_PROVIDERS)[number]

export type UnknownCostPolicy = 'stop' | 'allow_bounded'

export interface RuntimeProfile {
  provider: RuntimeProvider | string
  model: string
}

export type RuntimeProfiles = Record<RuntimeRole, RuntimeProfile>

export interface RuntimeLimits {
  timeout_s: number
  max_parallel: number
  max_tasks: number
  unknown_cost_policy: UnknownCostPolicy
}

export interface RuntimeConfiguration {
  revision: number
  profiles: RuntimeProfiles
  limits: RuntimeLimits
  updated_at?: string | null
}

export interface RuntimeIssue {
  code: string
  message: string
  remediation_id?: string | null
}

export interface RuntimeToolCapabilities {
  read_only: boolean
  workspace_write: boolean
}

export interface RuntimeTool {
  id: string
  installed: boolean
  version?: string | null
  runtime_status?: string | null
  import_status?: string | null
  capabilities?: RuntimeToolCapabilities
  auth?: 'present' | 'missing' | 'unknown' | string
  issues?: RuntimeIssue[]
}

export interface RuntimeHost {
  python_version?: string | null
  git_available?: boolean
  node_available?: boolean
  hermes_available?: boolean
  frontend_built?: boolean
}

export interface RuntimeAuthSourceDetail {
  id: string
  source?: string
}

export type RuntimeAuthSource = string | RuntimeAuthSourceDetail

export interface RuntimeReadiness {
  planning: boolean
  execution: boolean
  publishing: boolean
}

export interface RuntimeProbe {
  id: string
  profile: RuntimeRole | string
  provider: string
  model: string
  configuration_revision: number
  checked_at: string
  outcome: 'passed' | 'failed' | 'unsupported' | string
  message: string
}

export interface RuntimeInspection extends RuntimeConfiguration {
  /** Diagnostics call this field configuration_revision; settings calls it revision. */
  configuration_revision?: number
  checked_at?: string | null
  execution_mode?: string
  tools: RuntimeTool[]
  host?: RuntimeHost
  readiness?: RuntimeReadiness
  local_readiness?: RuntimeReadiness
  live_verified?: Partial<Record<RuntimeRole, boolean>>
  verification_note?: string | null
  auth_sources?: RuntimeAuthSource[]
  blockers: string[]
  last_probes: RuntimeProbe[]
}

export interface RuntimeProbeResponse extends RuntimeProbe {}

export function runtimeRoleLabel(role: RuntimeRole): string {
  return ({ planner: '规划', cheap: '低成本', standard: '标准', strong: '强能力' } as Record<RuntimeRole, string>)[role]
}

export function runtimeProviderLabel(provider: string): string {
  return ({ claude: 'Claude', codex: 'Codex', dsh: 'DSH' } as Record<string, string>)[provider] ?? provider
}

export function isRuntimeRole(value: string): value is RuntimeRole {
  return (RUNTIME_ROLES as readonly string[]).includes(value)
}
