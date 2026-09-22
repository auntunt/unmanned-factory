// 运维维护子系统的调用层。嵌入其他宿主时可通过 setMaintenanceApiBase 改前缀，
// 页面组件只经过这里访问后端，不直接拼路径。
import { request } from '../workspace/api'
import type { Graph, IntakeSource, Overview, Receipt, RepoView, Requirement } from './types'

let base = '/api/v2/maintenance'

/** 宿主适配：把子系统接到另一个前缀（同源反代）上。 */
export function setMaintenanceApiBase(next: string) { base = next.replace(/\/$/, '') }
export function maintenanceApiBase() { return base }

export interface CallOptions { csrfToken?: string; onUnauthorized?: () => void; signal?: AbortSignal }

const q = (projectId?: string) => projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''

export const maintenanceApi = {
  overview: (projectId: string | undefined, o: CallOptions = {}) =>
    request<Overview>(`${base}/overview${q(projectId)}`, o),
  graph: (projectId: string | undefined, o: CallOptions = {}) =>
    request<Graph>(`${base}/graph${q(projectId)}`, o),
  repos: (o: CallOptions = {}) => request<{ repos: RepoView[] }>(`${base}/repos`, o),
  repo: (projectId: string, o: CallOptions = {}) =>
    request<RepoView>(`${base}/repos/${encodeURIComponent(projectId)}`, o),
  registerRepo: (body: { source: string; name: string; branch?: string; credential_ref?: string; synthetic?: boolean }, o: CallOptions) =>
    request<RepoView>(`${base}/repos`, { ...o, method: 'POST', body }),
  probeRepo: (projectId: string, o: CallOptions) =>
    request<RepoView>(`${base}/repos/${encodeURIComponent(projectId)}/probe`, { ...o, method: 'POST', body: {} }),
  adoptChecks: (projectId: string, adopt: string[], o: CallOptions) =>
    request<RepoView>(`${base}/repos/${encodeURIComponent(projectId)}/checks`, { ...o, method: 'POST', body: { adopt } }),
  requirements: (projectId: string | undefined, o: CallOptions = {}) =>
    request<{ requirements: Requirement[] }>(`${base}/requirements${q(projectId)}`, o),
  submitRequirement: (body: { project_id: string; content: string; idempotency_key: string; attachments?: { name: string; ref: string }[] }, o: CallOptions) =>
    request<Receipt>(`${base}/requirements`, { ...o, method: 'POST', body }),
  dispatchRequirement: (id: string, o: CallOptions) =>
    request<Receipt>(`${base}/requirements/${encodeURIComponent(id)}/dispatch`, { ...o, method: 'POST', body: {} }),
  intakeSources: (o: CallOptions = {}) => request<{ sources: IntakeSource[] }>(`${base}/intake-sources`, o),
  markSynthetic: (projectId: string, synthetic: boolean, o: CallOptions) =>
    request<RepoView>(`${base}/repos/${encodeURIComponent(projectId)}/synthetic`, { ...o, method: 'POST', body: { synthetic } }),
  createIntakeSource: (body: { name: string; project_ids: string[]; auto_dispatch: boolean; synthetic?: boolean }, o: CallOptions) =>
    request<IntakeSource>(`${base}/intake-sources`, { ...o, method: 'POST', body }),
  revokeIntakeSource: (id: string, o: CallOptions) =>
    request<IntakeSource>(`${base}/intake-sources/${encodeURIComponent(id)}/revoke`, { ...o, method: 'POST', body: {} }),
  feedback: (taskId: string, content: string, o: CallOptions) =>
    request<{ task_id: string }>(`${base}/tasks/${encodeURIComponent(taskId)}/feedback`, { ...o, method: 'POST', body: { content } }),
  manifest: (o: CallOptions = {}) => request<Record<string, unknown>>(`${base}/manifest`, o),
}
