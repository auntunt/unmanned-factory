import ProjectKnowledge from './ProjectKnowledge'
import { nextRunAction } from './run-guidance'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import ProjectAgent from '../workspace/ProjectAgent'
import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import ChecksEditor, { validateChecks, type ChecksMap } from './ChecksEditor'
import { EmptyState, ErrorNotice, formatDate, PageHeader } from './ui'
import type { PageProps } from './ui'
import ProjectAutomation from './ProjectAutomation'
import ProjectMode from './ProjectMode'
import type { ProjectRecord } from './ProjectsPage'
import type { OverviewData } from './v3-types'
import ProjectLifecycle from './ProjectLifecycle'
import AttentionList from './AttentionList'
import { projectStage } from './project-stages'
import { runGuidance } from './run-guidance'
import { subscribeDataRefresh } from './data-refresh'
import './overview.css'
import './project-workspace.css'

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
  const [saved, setSaved] = useState(false)
  const [search] = useSearchParams()
  const location = useLocation()
  const returnRun = search.get('return_run')
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<ProjectRecord | null>(null)
  const [baseRevision, setBaseRevision] = useState(project.revision ?? 1)
  const dirtyRef = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  useEffect(() => { if (dirtyRef.current) return; setName(project.name); setBranch(project.base_branch); setChecks(project.checks ?? {}); setAutoIssues(Boolean(project.auto_issues)); setAutoPublish(Boolean(project.auto_publish)); setBudget(String(project.budget_usd ?? 10)); setBaseRevision(project.revision ?? 1) }, [project])

  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(null); setConflict(null); setSaved(false)
    const budgetValue = Number(budget)
    const checksError = validateChecks(checks)
    if (checksError) { setError(checksError); return }
    if (!Number.isFinite(budgetValue) || budgetValue <= 0) { setError('预算需要是大于 0 的数字。'); return }
    setBusy(true); controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller
    try {
      const next = await request<ProjectRecord>(`/api/v2/projects/${encodeURIComponent(String(project.id))}`, { method: 'PUT', csrfToken, onUnauthorized, signal: controller.signal, body: { revision: baseRevision, name: name.trim(), base_branch: branch.trim() || 'main', checks, auto_issues: autoIssues, auto_publish: autoPublish, budget_usd: budgetValue } })
      if (!controller.signal.aborted) { dirtyRef.current = false; setBaseRevision(next.revision ?? baseRevision); onSaved(next); setSaved(true) }
    } catch (cause) {
      if (!controller.signal.aborted) {
        if (cause instanceof WorkspaceApiError && cause.status === 409) {
          try { const payload = await request<{ projects: ProjectRecord[] }>(`/api/v2/projects`, { onUnauthorized, signal: controller.signal }); const latest = payload.projects.find((item) => String(item.id) === String(project.id)); if (latest) setConflict(latest); setError('项目设置已被其他人更新。请逐项审阅最新版本后，再明确选择如何重试。') } catch (refreshCause) { setError(`保存冲突，且无法读取最新项目设置：${errorText(refreshCause)}`) }
        } else setError(errorText(cause))
      }
    } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  useEffect(() => {
    const target = location.hash === '#project-budget' ? 'project-budget' : location.hash === '#project-checks' ? 'project-checks' : null
    if (!target) return
    const frame = requestAnimationFrame(() => {
      const element = document.getElementById(target)
      if (element instanceof HTMLDetailsElement) element.open = true
      element?.scrollIntoView({ block: 'center' })
      element?.querySelector('input')?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [location.hash])

  return <div className="pw-settings-stack">
    <form className="wb-card wb-form" onSubmit={save}>
      <div className="wb-card-head"><div><span className="wb-eyebrow">{project.name} · 项目设置</span><h2>预算与执行边界</h2><p>以下设置只属于这个项目。保存后用于后续规划和重试，不会自动继续已经暂停的运行。</p></div>{project.revision !== undefined && <span className="wb-revision">修订 {project.revision}</span>}</div>
      {false && <section className="pw-budget-field" id="project-budget" aria-labelledby="project-budget-label">
        <label htmlFor="project-budget-input" id="project-budget-label">单次运行预算（美元）<input id="project-budget-input" type="number" min="0.01" max="1000" step="0.01" required value={budget} onChange={(event) => { dirtyRef.current = true; setBudget(event.target.value); setSaved(false) }} /></label>
        <small>每条需求分别使用这条费用停止线。服务商已报告的累计费用超过此值后，工厂停止后续派发；它不是充值余额，也不是成员的月度 token 额度。</small>
        <small>正在进行的模型调用可能超过停止线。费用尚未返回时，按工作区的未知费用策略处理。</small>
      </section>}
      <div className="wb-form-grid wb-form-grid-two"><label>项目名称<input required maxLength={120} value={name} onChange={(event) => { dirtyRef.current = true; setName(event.target.value) }} /></label><label>基础分支<input required value={branch} onChange={(event) => { dirtyRef.current = true; setBranch(event.target.value) }} /></label><label>仓库<input value={project.repository} readOnly /></label><label>工作区<input value={project.workspace} readOnly /></label></div>
      <details className="wb-advanced" id="project-checks"><summary>验收检查与自动发布</summary><div className="wb-advanced-body"><ChecksEditor value={checks} onChange={(value) => { dirtyRef.current = true; setChecks(value) }} /><div className="wb-check-options"><label className="wb-checkbox"><input type="checkbox" checked={autoIssues} onChange={(event) => { dirtyRef.current = true; setAutoIssues(event.target.checked) }} />允许带 factory-ready 标签的问题自动执行</label><label className="wb-checkbox"><input type="checkbox" checked={autoPublish} onChange={(event) => { dirtyRef.current = true; setAutoPublish(event.target.checked) }} />允许通过验证后自动交付</label></div></div></details>
      {error && <ErrorNotice message={error} />}
      {conflict && <div className="wb-notice" role="alert"><p>最新服务器版本：修订 {conflict.revision ?? '—'} · 名称“{conflict.name}” · 分支 {conflict.base_branch} · 自动执行 {conflict.auto_issues ? '开' : '关'} · 自动交付 {conflict.auto_publish ? '开' : '关'}。</p><strong>最新验收检查</strong>{Object.entries(conflict.checks ?? {}).length ? <ul>{Object.entries(conflict.checks ?? {}).map(([checkName, argv]) => <li key={checkName}><code>{checkName}</code>：{argv.join(' ')}</li>)}</ul> : <p>没有配置验收检查。</p>}<button type="button" className="wb-button wb-button-secondary" onClick={() => { setBaseRevision(conflict.revision ?? baseRevision); setConflict(null); setError('已采用最新版本作为提交基线；页面保留你的修改，请确认后再次保存。') }}>我已审阅，保留我的修改并重试</button></div>}
      {saved && <div className="wb-notice" role="status">项目设置已保存。{returnRun ? '返回该运行后，可按新设置重新规划。' : '后续规划和重试将使用新设置。'}</div>}
      <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy || Boolean(conflict)}>{busy ? '保存中…' : conflict ? '请先审阅最新版本' : '保存项目设置'}</button>{returnRun && <Link className="wb-button wb-button-secondary" to={`/runs/${encodeURIComponent(returnRun)}?view=execution`}>{saved ? '返回该运行并重新规划 →' : '返回刚才的运行'}</Link>}</div>
    </form>
    <section className="wb-card"><h2>模型与执行设置</h2><p>费用和 token 额度由中转站统一管理。</p><Link to="/settings/runtime">配置模型、并行数与超时 →</Link></section>
  </div>
}

function RequirementForm({ project, csrfToken, onUnauthorized }: PageProps & { project: ProjectRecord }) {
  const navigate = useNavigate()
  const [value, setValue] = useState('')
  const [operation, setOperation] = useState('general')
  const submission = useRef({ signature: '', key: '' })
  const presets = [
    ['general', '新需求', '描述想得到的结果。'],
    ['bugfix', '修复 Bug', '描述实际表现、预期结果和复现步骤，可附错误信息或页面地址。'],
    ['startup', '启动排错', '例如：安装后启动失败；请检查依赖、环境配置和端口，修复并实际启动验证。'],
    ['release', '部署准备', '说明目标环境；助手检查构建、健康检查与回滚方式。缺少服务器连接时会明确说明。'],
    ['dependencies', '依赖维护', '描述安装错误或兼容性问题；助手定位必要更新并检查回归。'],
  ]
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    if (value.trim().length < 1) { setError('请先写下想做的事，简短描述也可以。'); return }
    if (busy) return
    const signature = JSON.stringify([project.id, operation, value.trim()])
    if (submission.current.signature !== signature) submission.current = { signature, key: crypto.randomUUID() }
    setBusy(true); controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller
    try { const run = await request<Run>('/api/v2/runs', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { project_id: project.id, request: value.trim(), operation, idempotency_key: submission.current.key } }); if (!controller.signal.aborted) { setValue(''); void navigate(`/runs/${encodeURIComponent(String(run.id))}`) } }
    catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  const selected = presets.find(([id]) => id === operation)!
  return <section id="project-work-request" className="wb-card wb-requirement-card">
    <div className="wb-card-head"><div><span className="wb-eyebrow">需求 · 维护 · 运维</span><h2>接下来让助手做什么</h2><p>选一种工作，补一句具体情况。助手沿用项目配置，执行、自测并提交验收。</p></div></div>
    <div className="wb-project-tabs" role="group" aria-label="工作类型">{presets.map(([id, label]) => <button type="button" key={id} aria-pressed={operation === id} disabled={busy} className={`wb-project-tab ${operation === id ? 'is-active' : ''}`} onClick={() => setOperation(id)}>{label}</button>)}</div>
    <form className="wb-form" onSubmit={submit}><label htmlFor="project-requirement">{selected[1]}说明<textarea id="project-requirement" required minLength={1} maxLength={50000} rows={4} value={value} disabled={busy} onChange={(event) => setValue(event.target.value)} placeholder={selected[2]} aria-describedby="operation-hint" /></label><p id="operation-hint" className="wb-runtime-note">{selected[2]}</p>
    {operation === 'bugfix' && <p className="wb-runtime-note">如果是下方已有任务失败，请先打开该任务继续修复，以保留工作成果与会话。</p>}
    {error && <ErrorNotice message={error} />}<div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy || value.trim().length < 1}>{busy ? '正在接收…' : `开始${selected[1]} →`}</button></div></form>
  </section>
}

function overviewNextRun(runs: Run[]) {
  return runs.find(run => ['needs_human', 'needs_clarification', 'awaiting_approval'].includes(run.status)) ?? runs.find(run => ['received', 'planning', 'queued', 'running', 'verifying', 'ready_for_review', 'publishing'].includes(run.status)) ?? runs[0]
}

export default function ProjectPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [policyRefresh, setPolicyRefresh] = useState(0)
  const isAdmin = user?.role !== 'member'
  const { projectId } = useParams<{ projectId: string }>()
  const [search, setSearch] = useSearchParams()
  const requestedTab = search.get('tab')
  const tab: ProjectTab = isAdmin && ['automation', 'agent', 'settings'].includes(requestedTab ?? '') ? requestedTab as ProjectTab : 'overview'
  const [project, setProject] = useState<ProjectRecord | null>(null)
  const [runs, setRuns] = useState<Run[] | null>(null)
  const currentRun = overviewNextRun(runs ?? [])
  const selectedStage = projectStage(search.get('stage') ?? (currentRun ? runGuidance(currentRun).stage : 'intake')).id
  const [overview, setOverview] = useState<OverviewData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [readiness, setReadiness] = useState<ProjectReadiness | null>(null)
  const [readinessError, setReadinessError] = useState<string | null>(null)
  const [memberCanRun, setMemberCanRun] = useState<boolean | null>(isAdmin ? true : null)
  const setTab = (next: ProjectTab) => { const params = new URLSearchParams(search); if (next === 'overview') params.delete('tab'); else params.set('tab', next); setSearch(params) }
  const setStage = (stage: string) => { const params = new URLSearchParams(search); params.delete('tab'); params.set('stage', stage); setSearch(params) }

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController(); setProject(null); setReadiness(null); setError(null); setMemberCanRun(isAdmin ? true : null)
    void request<{ projects: ProjectRecord[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal }).then((payload) => {
      if (!controller.signal.aborted) { const match = payload.projects.find((item) => String(item.id) === projectId); setProject(match ?? null); if (!match) setError('没有找到这个项目。') }
    }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) })
    if (!isAdmin) void request<{ projects: Array<{ id: string | number; member_ids?: Array<string | number> }> }>('/api/v3/team', { onUnauthorized, signal: controller.signal }).then((payload) => {
      if (!controller.signal.aborted) setMemberCanRun(payload.projects.some((item) => String(item.id) === projectId && (item.member_ids ?? []).some((id) => String(id) === String(user?.id))))
    }).catch((cause) => { if (!controller.signal.aborted) { setMemberCanRun(false); setError(errorText(cause)) } })
    return () => controller.abort()
  }, [isAdmin, onUnauthorized, projectId, user?.id])

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController(); setRuns(null); setOverview(null); setRunsError(null); setOverviewError(null)
    let busy = false
    const load = async () => {
      if (busy || controller.signal.aborted) return
      busy = true
      try {
        const summary = await request<OverviewData>(`/api/v3/overview?project_id=${encodeURIComponent(projectId)}`, { onUnauthorized, signal: controller.signal })
        if (!controller.signal.aborted) {
          if (summary.project_id !== projectId || !Array.isArray(summary.run_snapshots)) throw new Error('项目进度快照不完整，请刷新')
          setRuns(summary.run_snapshots); setOverview(summary); setRunsError(null); setOverviewError(null)
        }
      } catch (cause) { if (!controller.signal.aborted) setOverviewError(`刷新失败，保留上一份完整快照：${errorText(cause)}`) }
      busy = false
    }
    void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 5000)
    const unsubscribe = subscribeDataRefresh(() => void load())
    return () => { controller.abort(); window.clearInterval(timer); unsubscribe() }
  }, [onUnauthorized, projectId])

  useEffect(() => {
    if (!projectId || !project) return
    const controller = new AbortController(); setReadiness(null); setReadinessError(null)
    void request<ProjectReadiness>(`/api/v2/projects/${encodeURIComponent(projectId)}/readiness`, { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setReadiness(payload) }).catch((cause) => { if (!controller.signal.aborted) setReadinessError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized, projectId, project?.revision])

  const activeRun = useMemo(() => (runs ?? []).some((run) => ['received', 'planning', 'queued', 'running', 'verifying', 'publishing'].includes(run.status)), [runs])
  if (!project && error) return <div className="wb-page"><PageHeader title="项目" /><ErrorNotice message={error} /><Link className="wb-button wb-button-secondary" to="/projects">返回项目列表</Link></div>
  if (!project || String(project.id) !== projectId) return <div className="wb-page"><div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div></div>
  const encodedId = encodeURIComponent(String(project.id))
  const requirementForm = isAdmin || memberCanRun ? <RequirementForm project={project} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /> : <section className="wb-card wb-requirement-card"><p className="pw-stage-description">{memberCanRun === null ? '正在确认项目权限…' : '请管理员为你分配这个项目后，再发起新的需求。项目记录仍可查看。'}</p></section>

  return <div className="wb-page pw-project-page">
    <div className="pw-context"><Link to="/overview">工程总览</Link><span>/</span><Link to="/projects">项目</Link><span>/</span><span>{project.name}</span></div>
    <PageHeader title={project.name} description={project.managed_workspace ? '工作区已准备好，可以开始描述需求。' : `${project.repository} · ${project.base_branch}`} actions={isAdmin ? <span className="wb-runtime-note">计费由中转站管理</span> : <span className="wb-runtime-note">配置由项目管理员维护</span>} />
    <nav className="wb-project-tabs" aria-label="项目工作区">{(isAdmin ? [['overview', '工程闭环'], ['agent', '能力与知识'], ['automation', 'Auto 与能力'], ['settings', '项目设置']] as const : [['overview', '工程闭环']] as const).map(([key, label]) => <button key={key} aria-current={tab === key ? 'page' : undefined} className={`wb-project-tab ${tab === key ? 'is-active' : ''}`} onClick={() => setTab(key)}>{label}</button>)}</nav>
    <ProjectMode key={`${projectId}:${policyRefresh}`} projectId={String(project.id)} isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
    {tab === 'overview' && <>
      {error && <ErrorNotice message={error} />}{overviewError && <ErrorNotice message={overviewError} />}{runsError && <ErrorNotice message={runsError} />}
      {overview?.snapshot_at && <p className="wb-runtime-note">最近同步：{formatDate(overview.snapshot_at)} · 统计和任务使用同一份快照</p>}
      {overview && overview.runs > 0 && <section className="ov3-summary" aria-label="本项目进展"><div><span>已接收需求</span><strong>{overview.runs}</strong><small>只统计本项目</small></div><div><span>进行中</span><strong>{overview.active_runs}</strong><small>规划、执行或验证</small></div><div className={overview.attention_runs ? 'is-attention' : ''}><span>待处理</span><strong>{overview.attention_runs}</strong><small>需要处理的具体原因见下方</small></div><div><span>计费方式</span><strong>中转站管理</strong><small>本平台不再计费或限额</small></div></section>}
      {requirementForm}
      {currentRun && <section className="wb-card"><span className="wb-eyebrow">当前任务 · {runGuidance(currentRun).label}</span><h2>{currentRun.plan?.title || currentRun.request}</h2><p>{runGuidance(currentRun).summary}</p><Link className="wb-button wb-button-primary" to={nextRunAction(currentRun).href}>{nextRunAction(currentRun).label} →</Link></section>}
      {(overview?.attention ?? []).length > 0 && <section className="wb-card pw-attention"><div className="ov3-section-head"><div><span className="wb-eyebrow">本项目 · 待处理</span><h2>让工作继续的下一步</h2></div></div><AttentionList items={overview!.attention!.slice(0, 3)} /></section>}
      {runs?.length === 0 ? null : overview?.engineering && runs ? <ProjectLifecycle projectId={project.id} projectName={project.name} engineering={overview.engineering} runs={runs} selectedStage={selectedStage} onSelect={setStage} requirementForm={<a className="wb-text-link" href="#project-work-request">提交新需求或维护工作 ↑</a>} isAdmin={isAdmin} /> : !overviewError && <div className="wb-card ov3-loading" role="status" aria-label="正在读取本项目闭环"><span /><span /></div>}
      <details className="wb-card pw-preflight"><summary>项目准备与执行边界 · {readiness ? readiness.ready ? '可以开始' : '有配置需要处理' : '读取中'}</summary><div className="pw-preflight-body">
        <div className="wb-fact-grid"><div><span>基础分支</span><strong>{project.base_branch}</strong></div><div><span>验收检查</span><strong>{Object.keys(project.checks ?? {}).length} 条</strong></div><div><span>运行状态</span><strong>{activeRun ? '有进行中的运行' : '当前无执行任务'}</strong></div></div>
        <p className="wb-runtime-note">{project.workspace}</p>{readinessError && <ErrorNotice message={readinessError} />}
        {readiness && <div className="wb-project-readiness-list">{readiness.checks.map((check) => <div className="wb-project-readiness-row" key={check.id}><span className={`wb-readiness-dot ${check.status === 'ok' ? 'is-ready' : check.status === 'warning' ? 'is-warning' : 'is-blocked'}`} /><span><strong>{check.label}</strong><small>{check.message}</small></span></div>)}</div>}
        {isAdmin && <Link className="wb-text-link" to={`/projects/${encodedId}?tab=settings#project-checks`}>配置验收检查与执行边界 →</Link>}
      </div></details>
      <section className="wb-card pw-runs-list"><div className="wb-card-head"><div><span className="wb-eyebrow">本项目 · 需求档案</span><h2>每条需求的状态与下一步</h2></div><Link className="wb-text-link" to={`/runs?project_id=${encodedId}`}>全部运行 →</Link></div>
        {runs?.length === 0 && <EmptyState title="还没有需求记录" description="在需求澄清阶段提交这个项目的第一个目标。" />}
        {runs && runs.length > 0 && <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>需求</th><th>目前状态与原因</th><th>下一步</th></tr></thead><tbody>{runs.slice(0, 20).map((run) => { const guidance = runGuidance(run); return <tr key={String(run.id)}><td><Link className="wb-table-link" to={guidance.primaryHref}>{run.plan?.title || run.request.slice(0, 90)}</Link><small>{formatDate(run.updated_at)}</small></td><td><strong>{guidance.label}</strong><div className="pw-run-reason">{guidance.summary}</div></td><td><Link className="wb-text-link" to={nextRunAction(run).href}>{nextRunAction(run).label} →</Link></td></tr> })}</tbody></table></div>}
      </section>
    </>}
    {tab === 'agent' && isAdmin && <ProjectKnowledge projectId={project.id} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}
    {tab === 'agent' && isAdmin && <details className="wb-card wb-agent-card"><summary className="pk-reference-summary">项目档案、知识原文与代码索引</summary><div className="wb-project-agent"><ProjectAgent key={String(project.id)} projectId={project.id} repository={project.repository} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /></div></details>}
    {tab === 'automation' && isAdmin && <ProjectAutomation key={String(project.id)} projectId={String(project.id)} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onPolicySaved={() => setPolicyRefresh((value) => value + 1)} />}
    {tab === 'settings' && isAdmin && <EditSettings project={project} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onSaved={setProject} />}
  </div>
}
