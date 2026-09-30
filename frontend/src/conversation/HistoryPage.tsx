import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { statusLabel, formatDate, errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { headState } from './run-state'
import './conversation.css'
import './enterprise-workbench.css'

const PILL: Record<string, string> = { active: 'is-active', wait: 'is-wait', paused: 'is-wait', fail: 'is-fail', done: 'is-done' }
const FILTERS = [{ key: 'all', label: '最近任务' }, { key: 'attention', label: '待处理' }, { key: 'active', label: '进行中' }, { key: 'done', label: '已结束' }] as const
export function historyGroup(run: Run): string {
  const head = headState(run.status)
  if (head === 'active') return 'active'
  if (head === 'done' || run.retry_run_id || ['cancelled', 'discarded'].includes(run.status)) return 'done'
  return ['wait', 'paused', 'fail'].includes(head) ? 'attention' : 'unknown'
}

/** Read-only operator queue; failed refreshes retain the last successful records. */
export default function HistoryPage({ onUnauthorized, pollMs = 15000 }: PageProps & { pollMs?: number }) {
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null)
  const [params, setParams] = useSearchParams()
  const controller = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  const filter = FILTERS.some(f => f.key === params.get('status')) ? params.get('status')! : 'all'
  const query = params.get('q') || ''
  const change = (key: string, value: string) => { const next = new URLSearchParams(params); if (value && !(key === 'status' && value === 'all')) next.set(key, value); else next.delete(key); setParams(next, { replace: true }) }
  const load = useCallback(async () => {
    controller.current?.abort()
    const c = new AbortController(); controller.current = c
    inFlight.current = true; setLoading(true)
    try {
      const result = await request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized, signal: c.signal })
      if (c.signal.aborted) return
      setRuns([...result.runs].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))))
      setError(null); setRefreshedAt(new Date().toISOString())
    } catch (cause) { if (!c.signal.aborted) setError(errorText(cause)) }
    finally { if (!c.signal.aborted) { inFlight.current = false; setLoading(false) } }
  }, [onUnauthorized])
  useEffect(() => {
    void load()
    const timer = pollMs > 0 ? setInterval(() => { if (document.visibilityState !== 'hidden' && !inFlight.current) void load() }, pollMs) : undefined
    return () => { clearInterval(timer); controller.current?.abort() }
  }, [load, pollMs])
  const search = query.trim().toLocaleLowerCase()
  const visible = runs?.filter(run => (filter === 'all' || historyGroup(run) === filter) && [run.id, run.project_id, run.plan?.title, run.request].join(' ').toLocaleLowerCase().includes(search)) ?? []
  return (
    <div className="cv-page ew-history">
      <header className="ew-page-head"><div><span className="ew-eyebrow">执行与交付</span><h1>历史作品</h1><p className="cv-page-sub">集中查看任务状态、处理阻塞，回到原任务核对成果与证据。</p></div><Link className="cv-btn cv-btn-primary" to="/">新建任务 <Icon name="plus" width={16} height={16} /></Link></header>
      <div className="ew-filters" role="group" aria-label="筛选任务状态">{FILTERS.map(item => <button key={item.key} className={'ew-filter' + (filter === item.key ? ' is-selected' : '')} aria-pressed={filter === item.key} onClick={() => change('status', item.key)}><span>{item.label}</span><strong>{runs ? (item.key === 'all' ? runs.length : runs.filter(run => historyGroup(run) === item.key).length) : '—'}</strong></button>)}</div>
      <div className="ew-toolbar"><label className="ew-search"><span>搜索任务</span><input type="search" value={query} onChange={e => change('q', e.target.value)} placeholder="需求、作品名称、任务或项目编号" /></label><button className="cv-btn cv-btn-secondary" disabled={loading} onClick={() => void load()}>{loading ? '正在刷新…' : '刷新列表'}</button></div>
      <p className="ew-refresh" role="status">{refreshedAt ? '最近同步 ' + formatDate(refreshedAt) + (pollMs > 0 ? ' · 页面可见时自动刷新' : '') : '读取当前账号可见的任务'}{runs && ' · 当前显示 ' + visible.length + ' 项'}</p>
      {error && <div className="cv-error" role="alert"><strong>{runs ? '同步失败，以下为上次成功读取的记录' : '任务列表暂时不可用'}</strong><span>{error}</span><button className="cv-btn cv-btn-secondary" disabled={loading} onClick={() => void load()}>重新读取</button></div>}
      {runs === null && !error && <div className="cv-loading" role="status"><span className="cv-spinner" />正在读取任务…</div>}
      {runs && runs.length === 0 && <div className="ew-empty"><Icon name="project" width={28} height={28} /><h2>暂无最近可见任务</h2><p>可以创建新任务，或进入已有项目查找更早的运行记录。</p><Link className="cv-btn cv-btn-primary" to="/">创建新任务</Link></div>}
      {runs && runs.length > 0 && visible.length === 0 && <div className="ew-empty"><h2>没有符合条件的任务</h2><p>试试其他关键词，或清除筛选查看全部记录。</p><button className="cv-btn cv-btn-secondary" onClick={() => { const next = new URLSearchParams(params); next.delete('status'); next.delete('q'); setParams(next, { replace: true }) }}>清除筛选</button></div>}
      {visible.length > 0 && <div className="cv-list" aria-label="任务列表">
        {visible.map(run => {
          const head = headState(run.status)
          return <Link className="cv-list-item" key={String(run.id)} to={'/runs/' + encodeURIComponent(String(run.id))}>
            <div className="cv-li-body"><strong>{run.plan?.title || run.request}</strong>{run.plan?.title && run.plan.title !== run.request && <small>{run.request.slice(0, 100)}{run.request.length > 100 ? '…' : ''}</small>}<span className="ew-record-id">任务 #{run.id}{run.project_id != null ? ' · 项目 #' + run.project_id : ''}{run.retry_run_id ? ' · 已有后续运行接手' : ''}</span></div>
            <div className="cv-li-meta"><span className={'cv-pill ' + (PILL[head] || '')}><i />{statusLabel(run.status)}</span>{run.revision ? <span>计划 {run.revision}</span> : null}<span>{formatDate(run.updated_at)}</span><Icon name="arrow" width={16} height={16} /></div>
          </Link>
        })}
      </div>}
      <p className="ew-refresh">本页检索服务端最近 200 条中的可见任务，数量不是全量统计。更早记录请进入<Link to="/projects">对应项目</Link>查看。<br />“已结束”包含完成、取消及已有后续运行接手的记录；实际交付和验收结论请在任务内核对。</p>
    </div>
  )
}
