import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { statusLabel, formatDate, errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { headState } from './run-state'
import './conversation.css'

const PILL: Record<string, string> = { active: 'is-active', wait: 'is-wait', paused: 'is-wait', fail: 'is-fail', done: 'is-done' }

/** 历史作品：过往需求的列表，点开回到原对话工作区。 */
export default function HistoryPage({ onUnauthorized }: PageProps) {
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const c = new AbortController()
    request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized, signal: c.signal })
      .then(r => setRuns([...r.runs].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))))
      .catch(cause => { if (!c.signal.aborted) setError(errorText(cause)) })
    return () => c.abort()
  }, [onUnauthorized])
  return (
    <div className="cv-page">
      <h1>历史作品</h1>
      <p className="cv-page-sub">选择一个作品，回到它的对话继续。</p>
      {error && <div className="cv-error" role="alert"><span>{error}</span></div>}
      {runs === null && !error && <div className="cv-loading"><span className="cv-spinner" />正在读取…</div>}
      {runs && runs.length === 0 && <div className="cv-page-empty">还没有作品。回到首页描述一个目标就能开始。</div>}
      {runs && runs.length > 0 && <div className="cv-list">
        {runs.map(run => {
          const head = headState(run.status)
          return <Link className="cv-list-item" key={String(run.id)} to={`/runs/${encodeURIComponent(String(run.id))}`}>
            <div className="cv-li-body"><strong>{run.plan?.title || run.request}</strong><small>{run.request.slice(0, 80)}{run.request.length > 80 ? '…' : ''}</small></div>
            <div className="cv-li-meta">
              <span className={`cv-pill ${PILL[head] || ''}`}><i />{statusLabel(run.status)}</span>
              {run.revision ? <span>计划 {run.revision}</span> : null}
              <span>{formatDate(run.updated_at)}</span>
              <Icon name="arrow" width={16} height={16} />
            </div>
          </Link>
        })}
      </div>}
    </div>
  )
}
