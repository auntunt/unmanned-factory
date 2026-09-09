import { nextRunAction } from './run-guidance'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import { runGuidance } from './run-guidance'
import type { V3Project } from './v3-types'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge, errorText, type PageProps } from './ui'
import './run-guidance.css'
import { subscribeDataRefresh } from './data-refresh'

const filters = [['all', '全部'], ['active', '进行中'], ['attention', '需要处理'], ['ready_for_review', '已验证'], ['published', '已发布'], ['failed', '异常与取消']] as const
type Filter = typeof filters[number][0]
function asFilter(value: string | null): Filter { return filters.some(([key]) => key === value) ? value as Filter : 'all' }

export default function RunsPage({ onUnauthorized }: PageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>(() => asFilter(searchParams.get('filter')))
  const [error, setError] = useState<string | null>(null)
  const [projects, setProjects] = useState<V3Project[]>([])
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [refreshIndex, setRefreshIndex] = useState(0)
  const projectId = searchParams.get('project_id') ?? ''
  useEffect(() => { setFilter(asFilter(searchParams.get('filter'))) }, [searchParams])
  useEffect(() => subscribeDataRefresh(() => setRefreshIndex((value) => value + 1)), [])
  useEffect(() => {
    const current = searchParams.get('filter')
    if (current === filter || (filter === 'all' && !current)) return
    const next = new URLSearchParams(searchParams)
    if (filter === 'all') next.delete('filter'); else next.set('filter', filter)
    setSearchParams(next, { replace: true })
  }, [filter, searchParams, setSearchParams])
  useEffect(() => {
    const controller = new AbortController()
    setError(null); setProjectsError(null)
    void Promise.allSettled([
      request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized, signal: controller.signal }),
      request<{ projects: V3Project[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal }),
    ]).then(([runResult, projectResult]) => {
      if (controller.signal.aborted) return
      if (runResult.status === 'fulfilled') setRuns(runResult.value.runs)
      else if (!(runResult.reason instanceof WorkspaceApiError && runResult.reason.status === 401)) setError(errorText(runResult.reason))
      if (projectResult.status === 'fulfilled') setProjects(projectResult.value.projects)
      else if (!(projectResult.reason instanceof WorkspaceApiError && projectResult.reason.status === 401)) setProjectsError(`项目名称加载失败：${errorText(projectResult.reason)}`)
    })
    const timer = window.setInterval(() => setRefreshIndex((value) => value + 1), 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [onUnauthorized, refreshIndex])
  const visible = useMemo(() => (runs ?? []).filter((run) => {
    const matchesFilter = filter === 'all' || filter === 'active' ? (filter === 'all' || ['received', 'planning', 'queued', 'running', 'verifying', 'publishing'].includes(run.status)) : filter === 'attention' ? ['needs_clarification', 'awaiting_approval', 'needs_human'].includes(run.status) : filter === 'failed' ? ['failed', 'discarded', 'cancelled'].includes(run.status) : run.status === filter
    const matchesProject = !projectId || String(run.project_id) === projectId
    const text = `${run.request} ${run.plan?.title ?? ''} ${run.id} ${projects.find((project) => String(project.id) === String(run.project_id))?.name ?? ''}`.toLowerCase()
    return matchesFilter && matchesProject && text.includes(query.trim().toLowerCase())
  }).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))), [filter, projectId, projects, query, runs])
  const projectNames = new Map(projects.map((project) => [String(project.id), project]))
  const setProject = (value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set('project_id', value); else next.delete('project_id')
    setSearchParams(next, { replace: true })
  }
  return <div className="wb-page">
    <PageHeader title="运行看板" description="按真实运行状态追踪需求、异常和交付证据。" actions={<><button className="wb-button wb-button-secondary" onClick={() => setRefreshIndex((value) => value + 1)}>刷新</button><Link className="wb-button wb-button-primary" to="/projects">提交新需求 <span aria-hidden="true">＋</span></Link></>} />
    <section className="wb-card wb-runs-toolbar" aria-label="运行筛选"><label className="wb-search"><span aria-hidden="true">⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索需求、项目或运行编号" aria-label="搜索运行" /></label><label className="wb-run-project-filter">项目<select aria-label="按项目筛选运行" value={projectId} onChange={(event) => setProject(event.target.value)}><option value="">全部项目</option>{projects.map((project) => <option key={String(project.id)} value={String(project.id)}>{project.name}</option>)}</select></label><div className="wb-filter-list" role="tablist" aria-label="运行状态"><div>{filters.map(([key, label]) => <button key={key} role="tab" aria-selected={filter === key} className={`wb-filter ${filter === key ? 'is-active' : ''}`} onClick={() => setFilter(key)}>{label}<span>{key === 'all' ? runs?.length ?? '—' : key === 'active' ? runs?.filter((run) => ['received', 'planning', 'queued', 'running', 'verifying', 'publishing'].includes(run.status)).length ?? '—' : key === 'attention' ? runs?.filter((run) => ['needs_clarification', 'awaiting_approval', 'needs_human'].includes(run.status)).length ?? '—' : runs?.filter((run) => key === 'failed' ? ['failed', 'discarded', 'cancelled'].includes(run.status) : run.status === key).length ?? '—'}</span></button>)}</div></div></section>
    {error && <ErrorNotice message={error} />}{projectsError && <ErrorNotice message={projectsError} />}
    {!runs && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {runs && visible.length === 0 && <div className="wb-card"><EmptyState title={query || projectId ? '没有匹配的运行' : '还没有运行'} description={query || projectId ? '换一个关键词、项目或状态筛选试试。' : '从项目提交一条需求后，运行现场会出现在这里。'} action={!query && !projectId ? <Link className="wb-button wb-button-primary" to="/projects">浏览项目</Link> : undefined} /></div>}
    {runs && visible.length > 0 && <section className="wb-card wb-run-table-card"><div className="wb-table-wrap"><table className="wb-table wb-runs-table"><thead><tr><th>需求</th><th>项目</th><th>状态与下一步</th><th>计划</th><th>更新时间</th><th /></tr></thead><tbody>{visible.map((run) => { const project = projectNames.get(String(run.project_id)); const guidance = runGuidance(run); return <tr key={String(run.id)}><td><Link className="wb-table-link" to={guidance.primaryHref}>{run.plan?.title || run.request.slice(0, 100)}</Link><small className="wb-mono">#{run.id}</small></td><td>{project ? <Link className="wb-project-inline" to={`/projects/${encodeURIComponent(String(project.id))}?stage=${guidance.stage}`}>{project.name}<small className="wb-mono">#{String(project.id).slice(0, 8)}</small></Link> : <span className="wb-project-inline"><span>项目暂未返回</span><small className="wb-mono">#{String(run.project_id).slice(0, 8)}</small></span>}</td><td><StatusBadge status={run.status} /><small className="wb-run-guidance"><strong>{guidance.label}</strong> · {guidance.summary}</small></td><td>{run.revision ? `v${run.revision}` : '—'}</td><td>{formatDate(run.updated_at)}</td><td><Link className="wb-button wb-button-secondary" to={nextRunAction(run).href} aria-label={`${guidance.primaryLabel}：${run.plan?.title || run.request}`}>{nextRunAction(run).label} →</Link></td></tr> })}</tbody></table></div></section>}
  </div>
}
