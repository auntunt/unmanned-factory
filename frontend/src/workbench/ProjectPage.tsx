import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import ProjectAgent from '../workspace/ProjectAgent'
import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import ChecksEditor, { validateChecks, type ChecksMap } from './ChecksEditor'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge } from './ui'
import type { PageProps } from './ui'
import ProjectAutomation from './ProjectAutomation'
import type { ProjectRecord } from './ProjectsPage'

type ProjectTab = 'overview' | 'automation' | 'agent' | 'settings'

interface ProjectReadiness {
  project_id: string | number
  ready: boolean
  checks: Array<{ id: string; label: string; status: 'ok' | 'blocked' | 'warning' | string; message: string }>
  checked_at?: string
}

function errorText(error: unknown): string {
  if (error instanceof WorkspaceApiError) return error.detail
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

function EditSettings({ project, csrfToken, onUnauthorized, onSaved }: PageProps & { project: ProjectRecord; onSaved: (project: ProjectRecord) => void }) {
  const [name, setName] = useState(project.name)
  const [branch, setBranch] = useState(project.base_branch)
  const [checks, setChecks] = useState<ChecksMap>(project.checks ?? {})
  const [autoIssues, setAutoIssues] = useState(Boolean(project.auto_issues))
  const [autoPublish, setAutoPublish] = useState(Boolean(project.auto_publish))
  const [budget, setBudget] = useState(String(project.budget_usd ?? 10))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  useEffect(() => { setName(project.name); setBranch(project.base_branch); setChecks(project.checks ?? {}); setAutoIssues(Boolean(project.auto_issues)); setAutoPublish(Boolean(project.auto_publish)); setBudget(String(project.budget_usd ?? 10)) }, [project])

  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    const budgetValue = Number(budget)
    const checksError = validateChecks(checks)
    if (checksError) { setError(checksError); return }
    if (!Number.isFinite(budgetValue) || budgetValue <= 0) { setError('预算需要是大于 0 的数字。'); return }
    setBusy(true); controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller
    try {
      const next = await request<ProjectRecord>(`/api/v2/projects/${encodeURIComponent(String(project.id))}`, { method: 'PUT', csrfToken, onUnauthorized, signal: controller.signal, body: { revision: project.revision ?? 1, name: name.trim(), base_branch: branch.trim() || 'main', checks, auto_issues: autoIssues, auto_publish: autoPublish, budget_usd: budgetValue } })
      if (!controller.signal.aborted) onSaved(next)
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  return <form className="wb-card wb-form" onSubmit={save}>
    <div className="wb-card-head"><div><span className="wb-eyebrow">项目设置</span><h2>基本信息</h2><p>仓库和工作区来自项目登记，保持不变。</p></div>{project.revision !== undefined && <span className="wb-revision">修订 {project.revision}</span>}</div>
    <div className="wb-form-grid wb-form-grid-two"><label>项目名称<input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label><label>基础分支<input required value={branch} onChange={(event) => setBranch(event.target.value)} /></label><label>仓库<input value={project.repository} readOnly /></label><label>工作区<input value={project.workspace} readOnly /></label><label>费用停止阈值（美元）<input type="number" min="0.01" max="1000" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} /><small>这是已上报费用的停止阈值，不保证供应商侧美元硬上限；未知费用策略在运行配置中设置。</small></label></div>
    <details className="wb-advanced"><summary>检查与自动化设置</summary><div className="wb-advanced-body"><ChecksEditor value={checks} onChange={setChecks} /><div className="wb-check-options"><label className="wb-checkbox"><input type="checkbox" checked={autoIssues} onChange={(event) => setAutoIssues(event.target.checked)} />允许带 factory-ready 标签的问题自动执行</label><label className="wb-checkbox"><input type="checkbox" checked={autoPublish} onChange={(event) => setAutoPublish(event.target.checked)} />允许通过验证后自动交付</label></div></div></details>
    {error && <ErrorNotice message={error} />}
    <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy}>{busy ? '保存中…' : '保存项目设置'}</button></div>
  </form>
}

function RequirementForm({ project, csrfToken, onUnauthorized }: PageProps & { project: ProjectRecord }) {
  const navigate = useNavigate()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    if (value.trim().length < 5) { setError('请至少描述一个清晰的工程目标。'); return }
    setBusy(true); controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller
    try { const run = await request<Run>('/api/v2/runs', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { project_id: project.id, request: value.trim() } }); if (!controller.signal.aborted) { setValue(''); void navigate(`/runs/${encodeURIComponent(String(run.id))}`) } }
    catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  return <section className="wb-card wb-requirement-card"><div className="wb-card-head"><div><span className="wb-eyebrow">需求入口</span><h2>告诉系统要完成什么</h2><p>先描述目标、范围和验收方式，系统会生成可确认的计划。</p></div></div><form className="wb-form" onSubmit={submit}><label htmlFor="project-requirement">需求描述<textarea id="project-requirement" required minLength={5} maxLength={50000} rows={6} value={value} onChange={(event) => setValue(event.target.value)} placeholder="例如：为订单服务增加批量取消接口，保留现有单笔取消行为，并补充回归检查。" /></label>{error && <ErrorNotice message={error} />}<div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy || value.trim().length < 5}>{busy ? '提交中…' : '提交需求 →'}</button></div></form></section>
}

export default function ProjectPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const isAdmin = user?.role !== 'member'
  const { projectId } = useParams<{ projectId: string }>()
  const [project, setProject] = useState<ProjectRecord | null>(null)
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [readiness, setReadiness] = useState<ProjectReadiness | null>(null)
  const [readinessError, setReadinessError] = useState<string | null>(null)
  const [tab, setTab] = useState<ProjectTab>('overview')
  const [memberCanRun, setMemberCanRun] = useState<boolean | null>(isAdmin ? true : null)

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController(); setProject(null); setRuns(null); setReadiness(null); setError(null); setRunsError(null); setReadinessError(null); setTab('overview'); setMemberCanRun(isAdmin ? true : null)
    void request<{ projects: ProjectRecord[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) { const match = payload.projects.find((item) => String(item.id) === String(projectId)); setProject(match ?? null); if (!match) setError('没有找到这个项目。') } }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) })
    void request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setRuns(payload.runs.filter((run) => String(run.project_id) === String(projectId))) }).catch((cause) => { if (!controller.signal.aborted) setRunsError(errorText(cause)) })
    if (!isAdmin) void request<{ projects: Array<{ id: string | number; member_ids?: Array<string | number> }> }>('/api/v3/team', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) { const assigned = payload.projects.some((item) => String(item.id) === String(projectId) && (item.member_ids ?? []).some((id) => String(id) === String(user?.id))); setMemberCanRun(assigned) } }).catch((cause) => { if (!controller.signal.aborted) { setMemberCanRun(false); setError(errorText(cause)) } })
    return () => controller.abort()
  }, [isAdmin, onUnauthorized, projectId, user?.id])

  useEffect(() => {
    if (!projectId || !project) return
    const controller = new AbortController(); setReadiness(null); setReadinessError(null)
    void request<ProjectReadiness>(`/api/v2/projects/${encodeURIComponent(String(projectId))}/readiness`, { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setReadiness(payload) }).catch((cause) => { if (!controller.signal.aborted) setReadinessError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized, projectId, project?.revision])

  const activeRun = useMemo(() => (runs ?? []).some((run) => ['received', 'planning', 'needs_clarification', 'awaiting_approval', 'queued', 'running', 'verifying', 'ready_for_review', 'publishing'].includes(run.status)), [runs])
  if (!project && error) return <div className="wb-page"><PageHeader title="项目" /><ErrorNotice message={error} /><Link className="wb-button wb-button-secondary" to="/projects">返回项目列表</Link></div>
  if (!project) return <div className="wb-page"><div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div></div>

  return <div className="wb-page wb-project-page">
    <PageHeader title={project.name} description={`${project.repository} · ${project.workspace}`} actions={<Link className="wb-button wb-button-secondary" to="/projects">返回项目</Link>} />
    <div className="wb-project-tabs" role="tablist" aria-label="项目视图">{(isAdmin ? [['overview', '概览'], ['automation', '自主运行'], ['agent', '项目代理'], ['settings', '设置']] as const : [['overview', '概览']] as const).map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} className={`wb-project-tab ${tab === key ? 'is-active' : ''}`} onClick={() => setTab(key)}>{label}</button>)}</div>
    {tab === 'overview' && <div className="wb-project-overview">{isAdmin || memberCanRun ? <RequirementForm project={project} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /> : memberCanRun === null ? <section className="wb-card wb-requirement-card"><div className="wb-readiness-loading">正在确认项目权限…</div></section> : <section className="wb-card wb-requirement-card"><div className="wb-card-head"><div><span className="wb-eyebrow">需求入口</span><h2>需要项目权限</h2><p>项目记录对小组共享；请管理员为你分配这个项目后，再发起新的需求运行。</p></div></div></section>}<section className="wb-card wb-runs-card"><div className="wb-card-head"><div><span className="wb-eyebrow">项目记录</span><h2>需求运行</h2></div><span className="wb-count-badge">{runs?.length ?? '—'}</span></div>{runsError && <ErrorNotice message={runsError} />}{runs && runs.length === 0 && <EmptyState title="还没有需求运行" description="提交一条需求后，规划和执行记录会出现在这里。" />}{runs && runs.length > 0 && <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>需求</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>{runs.map((run) => <tr key={String(run.id)}><td><Link className="wb-table-link" to={`/runs/${encodeURIComponent(String(run.id))}`}>{run.plan?.title || run.request.slice(0, 90)}</Link><small>运行 #{run.id}</small></td><td><StatusBadge status={run.status} /></td><td>{formatDate(run.updated_at)}</td><td><Link className="wb-row-arrow" to={`/runs/${encodeURIComponent(String(run.id))}`} aria-label="查看运行">→</Link></td></tr>)}</tbody></table></div>}</section><section className="wb-card wb-project-facts"><div className="wb-card-head"><div><span className="wb-eyebrow">项目信息</span><h2>执行边界</h2></div></div><div className="wb-fact-grid"><div><span>当前分支</span><strong>{project.base_branch}</strong></div><div><span>检查</span><strong>{Object.keys(project.checks ?? {}).length} 条</strong></div><div><span>运行状态</span><strong>{activeRun ? '有进行中的运行' : '当前空闲'}</strong></div><div><span>预算</span><strong>{project.budget_usd === undefined ? '—' : `$${project.budget_usd.toFixed(2)}`}</strong></div></div></section><section className="wb-card wb-readiness-card"><div className="wb-card-head"><div><span className="wb-eyebrow">运行前检查</span><h2>项目准备情况</h2><p>只检查仓库和配置，不执行项目命令。</p></div>{readiness && <span className={`wb-readiness-summary ${readiness.ready ? 'is-ready' : 'is-blocked'}`}>{readiness.ready ? '可以开始' : '需要处理'}</span>}</div>{readinessError && <ErrorNotice message={readinessError} />}{!readinessError && !readiness && <div className="wb-readiness-loading">正在读取准备情况…</div>}{readiness && <div className="wb-project-readiness-list">{readiness.checks.map((check) => <div className="wb-project-readiness-row" key={check.id}><span className={`wb-readiness-dot ${check.status === 'ok' ? 'is-ready' : check.status === 'warning' ? 'is-warning' : 'is-blocked'}`} /><span><strong>{check.label}</strong><small>{check.message}</small></span></div>)}</div>}</section></div>}
    {tab === 'agent' && isAdmin && <section className="wb-card wb-agent-card"><div className="wb-card-head"><div><span className="wb-eyebrow">工程上下文</span><h2>项目代理</h2><p>维护项目知识、代码索引和显式导入内容。</p></div></div><div className="wb-project-agent"><ProjectAgent key={String(project.id)} projectId={project.id} repository={project.repository} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /></div></section>}
    {tab === 'automation' && isAdmin && <ProjectAutomation projectId={String(project.id)} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}
    {tab === 'settings' && isAdmin && <EditSettings project={project} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onSaved={(next) => setProject(next)} />}
  </div>
}
