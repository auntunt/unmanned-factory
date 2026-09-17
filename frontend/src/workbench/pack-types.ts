/** Capability packs: a versioned, executable capability with its own evidence.
 *  Three states are kept apart on purpose — the version's lifecycle, whether this
 *  environment can run it, and how one execution actually went. The UI must never
 *  merge them into a single green "ready". */
export type PackLifecycle = 'draft' | 'building' | 'validating' | 'verified' | 'published' | 'retired' | 'needs_changes'
export type EnvStatus = 'unchecked' | 'checking' | 'ready' | 'unavailable'
export type TaskStatus = 'queued' | 'running' | 'waiting_input' | 'succeeded' | 'failed' | 'cancelled'

export interface PackFile { path: string; sha256: string; size: number; role: 'program' | 'fixture'; material_scope?: string | null }
export interface SupportRow { format: string; status: 'supported' | 'partial' | 'unsupported'; evidence: string }
export interface PackManifest {
  name: string; purpose: string
  tool: { name: string; entrypoint: string; runtime: string; timeout_seconds: number }
  dependency_lock: { python: string; packages: string[] }
  support_matrix: SupportRow[]
  evaluation_policy: { test_set: string; required: boolean }
}
export interface EvaluationCase { id: string; passed: boolean; reason?: string; status?: string; error_code?: string | null; duration_ms?: number; isolation?: string }
export interface Evaluation {
  id: string; content_digest: string; created_at: string; passed: boolean; summary: string
  cases: EvaluationCase[]; test_set?: string; scope?: SupportRow[]
  environment?: { status?: string; python?: string; platform?: string; sandbox?: string; missing?: string[] }
}
export interface PackDraft {
  revision: number; lifecycle: PackLifecycle; blocked_reason?: string | null
  manifest: PackManifest | null; files: PackFile[]; content_digest: string
  excluded: Array<{ path: string; reason: string }>; source_task_id?: string
  evaluations?: Evaluation[]; published_version_id?: string
}
export interface PackVersion {
  id: string; pack_id: string; version: number; content_digest: string; created_at: string
  manifest: PackManifest; files: PackFile[]; evaluation_id: string; support_matrix: SupportRow[]
  lifecycle: PackLifecycle; source_task_id?: string
}
export interface PackSummary { id: string; name: string; purpose: string; owner_id: string; updated_at: string; published_version?: number | null }
export interface PackBinding {
  id: string; agent_id: string; pack_id: string; pack_name: string; version_id: string; version: number
  revision: number; latest_version: number; upgrade_available: boolean; content_digest: string
  environment?: { status: EnvStatus; missing?: string[] }
}
export interface PackDetail extends PackSummary {
  draft: PackDraft | null; versions: PackVersion[]; evaluations: Evaluation[]
  bindings: PackBinding[]; environments: Record<string, { status: EnvStatus; missing?: string[]; checked_at?: string }>
  can_maintain: boolean
}
export interface PackArtifact { id: string; name: string; size: number; validation_status: string; kind?: string }
export interface PackTask {
  id: string; pack_id: string; agent_id: string; status: TaskStatus; validation_status: string
  error_code?: string | null; error?: string | null; result?: Record<string, unknown> | null
  outputs: PackArtifact[]; inputs: PackArtifact[]; diagnostics?: string[]
  snapshot: { version: number; version_id: string; content_digest: string; tool: string }
  created_at: string
}

export const LIFECYCLE_LABEL: Record<PackLifecycle, string> = {
  draft: '草稿', building: '构建中', validating: '验证中', verified: '待发布',
  published: '已发布', retired: '已停用', needs_changes: '待修复',
}
export const ENV_LABEL: Record<EnvStatus, string> = {
  unchecked: '尚未检查', checking: '检查中', ready: '环境可用', unavailable: '环境缺依赖',
}
export const SUPPORT_LABEL: Record<SupportRow['status'], string> = {
  supported: '已验证', partial: '部分支持', unsupported: '暂不支持',
}
export const packsBase = '/api/v4/capability-packs'
