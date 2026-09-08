import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '../workspace/api'
import type { Project, Run } from '../workspace/types'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge } from './ui'
import type { PageProps } from './ui'

interface RuntimeSummary {
  checked_at?: string
  readiness?: { planning?: boolean; execution?: boolean; publishing?: boolean }
  local_readiness?: { planning?: boolean; execution?: boolean; publishing?: boolean }
  configuration_revision?: number
  blockers?: string[]
  last_probes?: Array<{ profile?: string; outcome?: string; configuration_revision?: number }>
  profiles?: Record<string, { provider?: string; model?: string }>
  tools?: Array<{ id?: string; installed?: boolean; auth?: string; issues?: Array<{ message?: string }> }>
}

type LoadState<T> = { value: T | null; loading: boolean; error: string | null }

const initialState = <T,>(): LoadState<T> => ({ value: null, loading: true, error: null })

function taskItems(run: Run): Array<{ status?: string }> {
  const value = run.tasks
  if (Array.isArray(value)) return value.filter((item): item is { status?: string } => Boolean(item && typeof item === 'object'))
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    for (const key of ['tasks', 'items']) {
      if (Array.isArray(record[key])) return record[key].filter((item): item is { status?: string } => Boolean(item && typeof item === 'object'))
    }
  }
  return run.plan?.tasks ?? []
}

export function taskGroupCounts(runs: Run[]): { total: number; active: number; completed: number; blocked: number } {
  const items = runs.flatMap(taskItems)
  return {
    total: items.length,
    active: items.filter((item) => ['queued', 'running'].includes(item.status ?? '')).length,
    completed: items.filter((item) => ['completed', 'verified'].includes(item.status ?? '')).length,
    blocked: items.filter((item) => ['blocked', 'failed', 'cancelled'].includes(item.status ?? '')).length,
  }
}

function errorText(error: unknown): string {
  if (error && typeof error === 'object' && 'detail' in error && typeof error.detail === 'string') return error.detail
  return error instanceof Error ? error.message : '读取失败，请稍后重试。'
}

function RuntimeCard({ state }: { state: LoadState<RuntimeSummary> }) {
  const runtime = state.value
  const readinessText = (key: 'planning' | 'execution' | 'publishing'): string => {
    if (runtime?.readiness?.[key]) return '连接已验证'
    if (runtime?.blockers?.length && runtime.local_readiness?.[key] === false) return '有本地阻塞'
    return '尚未验证连接'
  }
  return <section className="wb-card wb-runtime-card">
    <div className="wb-card-head"><div><span className="wb-eyebrow">运行配置</span><h2>执行准备情况</h2></div><Link className="wb-text-link" to="/settings/runtime">查看配置 →</Link></div>
    {state.loading && <div className="wb-skeleton-line" />}
    {state.error && <ErrorNotice message={state.error} />}
    {!state.loading && !state.error && runtime && <>
      <div className="wb-readiness-grid">
        {(['planning', 'execution', 'publishing'] as const).map((key) => <div className="wb-readiness-item" key={key}><span className={`wb-readiness-dot ${runtime.readiness?.[key] ? 'is-ready' : 'is-blocked'}`} /><span>{key === 'planning' ? '规划' : key === 'execution' ? '执行' : '交付'}</span><strong>{readinessText(key)}</strong></div>)}
      </div>
      <p className="wb-card-foot">配置修订 {runtime.configuration_revision ?? '—'}{runtime.checked_at ? ` · 检查于 ${formatDate(runtime.checked_at)}` : ''}</p>
    </>}
    {!state.loading && !state.error && !runtime && <EmptyState title="还没有运行配置" description="完成运行角色配置后，系统才会展示可执行范围。" action={<Link className="wb-button wb-button-secondary" to="/settings/runtime">打开运行配置</Link>} />}
  </section>
}

export default function OverviewPage({ onUnauthorized }: PageProps) {
  const [projects, setProjects] = useState<LoadState<Project[]>>(initialState)
  const [runs, setRuns] = useState<LoadState<Run[]>>(initialState)
  const [runtime, setRuntime] = useState<LoadState<RuntimeSummary>>(initialState)

  useEffect(() => {
    const controller = new AbortController()
    setProjects(initialState()); setRuns(initialState()); setRuntime(initialState())
    void request<{ projects: Project[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal })
      .then((payload) => { if (!controller.signal.aborted) setProjects({ value: payload.projects, loading: false, error: null }) })
      .catch((error) => { if (!controller.signal.aborted) setProjects({ value: null, loading: false, error: errorText(error) }) })
    void request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized, signal: controller.signal })
      .then((payload) => { if (!controller.signal.aborted) setRuns({ value: payload.runs, loading: false, error: null }) })
      .catch((error) => { if (!controller.signal.aborted) setRuns({ value: null, loading: false, error: errorText(error) }) })
    void request<RuntimeSummary>('/api/v2/runtime', { onUnauthorized, signal: controller.signal })
      .then((payload) => { if (!controller.signal.aborted) setRuntime({ value: payload, loading: false, error: null }) })
      .catch((error) => { if (!controller.signal.aborted) setRuntime({ value: null, loading: false, error: errorText(error) }) })
    return () => controller.abort()
  }, [onUnauthorized])

  const taskCounts = useMemo(() => taskGroupCounts(runs.value ?? []), [runs.value])
  const attentionRuns = useMemo(() => (runs.value ?? []).filter((run) => ['needs_clarification', 'awaiting_approval', 'ready_for_review', 'needs_human'].includes(run.status)), [runs.value])
  const actionRuns = useMemo(() => attentionRuns.slice(0, 6), [attentionRuns])
  const recentRuns = useMemo(() => [...(runs.value ?? [])].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 5), [runs.value])

  return <div className="wb-page">
    <PageHeader title="工作台" description="从需求到交付，集中查看项目进度和需要你确认的事项。" actions={<Link className="wb-button wb-button-primary" to="/projects">开始新需求 <span aria-hidden="true">＋</span></Link>} />
    <div className="wb-stat-grid wb-stat-grid-four">
      <div className="wb-stat-card"><span>项目</span><strong>{projects.loading ? '—' : projects.value?.length ?? 0}</strong><small>已登记的工程</small></div>
      <div className="wb-stat-card"><span>运行</span><strong>{runs.loading ? '—' : runs.value?.length ?? 0}</strong><small>全部需求运行</small></div>
      <div className="wb-stat-card"><span>进行中的任务</span><strong>{runs.loading ? '—' : taskCounts.active}</strong><small>{taskCounts.total ? `共 ${taskCounts.total} 个任务` : '等待运行产生任务'}</small></div>
      <div className="wb-stat-card"><span>需要确认</span><strong>{runs.loading ? '—' : attentionRuns.length}</strong><small>需要你作决定的运行</small></div>
    </div>
    <div className="wb-overview-grid">
      <section className="wb-card wb-action-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">下一步</span><h2>需要确认</h2></div><span className="wb-count-badge">{runs.loading ? '—' : attentionRuns.length}</span></div>
        {runs.error && <ErrorNotice message={runs.error} />}
        {!runs.loading && !runs.error && actionRuns.length === 0 && <EmptyState title="暂时没有待处理事项" description="新的澄清、审批或交付复核会出现在这里。" />}
        <div className="wb-action-list">{actionRuns.map((run) => <Link to={`/runs/${encodeURIComponent(String(run.id))}`} className="wb-action-row" key={String(run.id)}><span className="wb-action-icon">{run.status === 'ready_for_review' ? '✓' : '?'}</span><span className="wb-action-copy"><strong>{run.plan?.title || run.request.slice(0, 80)}</strong><small>{run.project_id ? `项目 ${run.project_id} · ` : ''}{formatDate(run.updated_at)}</small></span><StatusBadge status={run.status} /><span className="wb-row-arrow">→</span></Link>)}</div>
      </section>
      <RuntimeCard state={runtime} />
    </div>
    <section className="wb-card wb-recent-card">
      <div className="wb-card-head"><div><span className="wb-eyebrow">最近活动</span><h2>运行</h2></div><Link className="wb-text-link" to="/projects">查看项目 →</Link></div>
      {runs.loading && <div className="wb-list-placeholder"><span /><span /><span /></div>}
      {runs.error && <ErrorNotice message={runs.error} />}
      {!runs.loading && !runs.error && recentRuns.length === 0 && <EmptyState title="还没有运行记录" description="选择一个项目，提交第一条需求开始工作。" action={<Link className="wb-button wb-button-secondary" to="/projects">浏览项目</Link>} />}
      {!runs.loading && !runs.error && recentRuns.length > 0 && <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>需求</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>{recentRuns.map((run) => <tr key={String(run.id)}><td><Link className="wb-table-link" to={`/runs/${encodeURIComponent(String(run.id))}`}>{run.plan?.title || run.request.slice(0, 100)}</Link><small>运行 #{run.id}</small></td><td><StatusBadge status={run.status} /></td><td>{formatDate(run.updated_at)}</td><td><Link className="wb-row-arrow" to={`/runs/${encodeURIComponent(String(run.id))}`} aria-label="查看运行">→</Link></td></tr>)}</tbody></table></div>}
    </section>
  </div>
}
