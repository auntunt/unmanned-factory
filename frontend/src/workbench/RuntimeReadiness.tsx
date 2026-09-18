/**
 * Member-facing runtime readiness view.
 *
 * Shows only availability status (planning / execution / publishing)
 * and blocker messages returned by the role-filtered GET /api/v2/runtime.
 * No profiles, tools diagnostics, host info, or auth_sources are displayed.
 */
import { LoadingCard } from './presentation'
import { useCallback, useEffect, useState } from 'react'
import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText, type PageProps } from './ui'
import type { RuntimeReadiness as RuntimeReadinessType } from './runtime-types'
import './detail.css'

/** Shape returned by GET /api/v2/runtime for a member role. */
export interface MemberRuntimeStatus {
  readiness?: RuntimeReadinessType | null
  local_readiness?: RuntimeReadinessType | null
  blockers: string[]
  configuration_revision?: number
}

function readinessLabel(ready: boolean | undefined): string {
  if (ready === true) return '可用'
  if (ready === false) return '不可用'
  return '未知'
}

export default function RuntimeReadiness({ onUnauthorized }: PageProps) {
  const [status, setStatus] = useState<MemberRuntimeStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const data = await request<MemberRuntimeStatus>('/api/v2/runtime', { onUnauthorized, signal })
      if (!signal?.aborted) setStatus(data)
    } catch (cause) {
      if (!signal?.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) {
        setError(errorText(cause))
      }
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [onUnauthorized])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  if (loading && !status) {
    return <div className="wb-detail-page"><LoadingCard label="正在检查运行环境可用性" /></div>
  }
  if (!status) {
    return (
      <div className="wb-detail-page">
        {error ? <ErrorNotice message={error} /> : <EmptyState title="运行状态暂不可用" description="服务端没有返回运行状态信息。请稍后重试。" />}
      </div>
    )
  }

  const readiness = status.readiness
  const blockers = status.blockers ?? []

  return (
    <div className="wb-detail-page">
      <PageHeader title="运行环境状态" description="显示当前平台的可用性。配置由管理员管理。" />
      {error && <ErrorNotice message={error} />}

      <section className="wb-detail-card wb-runtime-section" aria-labelledby="member-readiness-title">
        <h2 id="member-readiness-title">可用性</h2>
        {readiness ? (
          <div className="wb-readiness-list">
            <div className="wb-side-stat"><span>规划</span><span>{readinessLabel(readiness.planning)}</span></div>
            <div className="wb-side-stat"><span>执行</span><span>{readinessLabel(readiness.execution)}</span></div>
            <div className="wb-side-stat"><span>发布</span><span>{readinessLabel(readiness.publishing)}</span></div>
          </div>
        ) : (
          <p className="wb-runtime-note">可用性状态暂未返回。</p>
        )}
      </section>

      {blockers.length > 0 && (
        <section className="wb-detail-card wb-runtime-section" aria-labelledby="member-blockers-title">
          <h2 id="member-blockers-title">当前缺口</h2>
          <ul className="wb-tool-issues">
            {blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
          </ul>
        </section>
      )}
    </div>
  )
}
