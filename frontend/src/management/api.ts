// 管理视图（/api/v5/*）调用层，风格与 workspace/api.ts + maintenance/api.ts 一致：
// 页面组件只经过这里访问后端，不直接拼路径。
import { request } from '../workspace/api'
import type { Overview, OrgTree, ProjectDetail, UnitKind, Workspaces } from './types'

export interface CallOptions { csrfToken?: string; onUnauthorized?: () => void; signal?: AbortSignal }

function query(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export const managementApi = {
  workspaces: (o: CallOptions = {}) => request<Workspaces>('/api/v5/me/workspaces', o),
  overview: (unitId: string | undefined, days: number, o: CallOptions = {}) =>
    request<Overview>(`/api/v5/management/overview${query({ unit_id: unitId, days })}`, o),
  project: (projectId: string, o: CallOptions = {}) =>
    request<ProjectDetail>(`/api/v5/management/projects/${encodeURIComponent(projectId)}`, o),

  orgTree: (o: CallOptions = {}) => request<OrgTree>('/api/v5/org', o),
  createUnit: (body: { name: string; kind: UnitKind; parent_id: string | null }, o: CallOptions) =>
    request<unknown>('/api/v5/org/units', { ...o, method: 'POST', body }),
  updateUnit: (unitId: string, body: { name?: string; parent_id?: string | null }, o: CallOptions) =>
    request<unknown>(`/api/v5/org/units/${encodeURIComponent(unitId)}`, { ...o, method: 'PATCH', body }),
  deleteUnit: (unitId: string, o: CallOptions) =>
    request<{ ok: true }>(`/api/v5/org/units/${encodeURIComponent(unitId)}`, { ...o, method: 'DELETE' }),
  bindProject: (projectId: string, unitId: string, o: CallOptions) =>
    request<{ ok: true }>(`/api/v5/org/projects/${encodeURIComponent(projectId)}`, { ...o, method: 'PUT', body: { unit_id: unitId } }),
  unbindProject: (projectId: string, o: CallOptions) =>
    request<{ ok: true }>(`/api/v5/org/projects/${encodeURIComponent(projectId)}`, { ...o, method: 'DELETE' }),
  grantScope: (userId: number, unitId: string, o: CallOptions) =>
    request<{ ok: true }>('/api/v5/org/scopes', { ...o, method: 'POST', body: { user_id: userId, unit_id: unitId } }),
  revokeScope: (userId: number, unitId: string, o: CallOptions) =>
    request<{ ok: true }>(`/api/v5/org/scopes/${encodeURIComponent(String(userId))}/${encodeURIComponent(unitId)}`, { ...o, method: 'DELETE' }),
}
