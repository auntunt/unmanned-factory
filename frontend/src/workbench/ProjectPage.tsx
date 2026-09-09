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
      <section className="pw-budget-field" id="project-budget" aria-labelledby="project-budget-label">
        <label htmlFor="project-budget-input" id="project-budget-label">单次运行预算（美元）<input id="project-budget-input" type="number" min="0.01" max="1000" step="0.01" required value={budget} onChange={(event) => { dirtyRef.current = true; setBudget(event.target.value); setSaved(false) }} /></label>
        <small>每条需求分别使用这条费用停止线。服务商已报告的累计费用超过此值后，工厂停止后续派发；它不是充值余额，也不是成员的月度 token 额度。</small>
        <small>正在进行的模型调用可能超过停止线。费用尚未返回时，按工作区的未知费用策略处理。</small>
      </section>
      <div className="wb-form-grid wb-form-grid-two"><label>项目名称<input required maxLength={120} value={name} onChange={(event) => { dirtyRef.current = true; setName(event.target.value) }} /></label><label>基础分支<input required value={branch} onChange={(event) => { dirtyRef.current = true; setBranch(event.target.value) }} /></label><label>仓库<input value={project.repository} readOnly /></label><label>工作区<input value={project.workspace} readOnly /></label></div>
      <details className="wb-advanced" id="project-checks"><summary>验收检查与自动发布</summary><div className="wb-advanced-body"><ChecksEditor value={checks} onChange={(value) => { dirtyRef.current = true; setChecks(value) }} /><div className="wb-check-options"><label className="wb-checkbox"><input type="checkbox" checked={autoIssues} onChange={(event) => { dirtyRef.current = true; setAutoIssues(event.target.checked) }} />允许带 factory-ready 标签的问题自动执行</label><label className="wb-checkbox"><input type="checkbox" checked={autoPublish} onChange={(event) => { dirtyRef.current = true; setAutoPublish(event.target.checked) }} />允许通过验证后自动交付</label></div></div></details>
      {error && <ErrorNotice message={error} />}
      {conflict && <div className="wb-notice" role="alert"><p>最新服务器版本：修订 {conflict.revision ?? '—'} · 名称“{conflict.name}” · 分支 {conflict.base_branch} · 预算 ${Number(conflict.budget_usd ?? 0).toFixed(2)} · 自动执行 {conflict.auto_issues ? '开' : '关'} · 自动交付 {conflict.auto_publish ? '开' : '关'}。</p><strong>最新验收检查</strong>{Object.entries(conflict.checks ?? {}).length ? <ul>{Object.entries(conflict.checks ?? {}).map(([checkName, argv]) => <li key={checkName}><code>{checkName}</code>：{argv.join(' ')}</li>)}</ul> : <p>没有配置验收检查。</p>}<button type="button" className="wb-button wb-button-secondary" onClick={() => { setBaseRevision(conflict.revision ?? baseRevision); setConflict(null); setError('已采用最新版本作为提交基线；页面保留你的修改，请确认后再次保存。') }}>我已审阅，保留我的修改并重试</button></div>}
      {saved && <div className="wb-notice" role="status">项目设置已保存，单次运行预算为 ${Number(project.budget_usd ?? budget).toFixed(2)}。{returnRun ? '返回该运行后，可按新设置重新规划。' : '后续规划和重试将使用新设置。'}</div>}
      <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy || Boolean(conflict)}>{busy ? '保存中…' : conflict ? '请先审阅最新版本' : '保存预算与项目设置'}</button>{returnRun && <Link className="wb-button wb-button-secondary" to={`/runs/${encodeURIComponent(returnRun)}?view=execution`}>{saved ? '返回该运行并重新规划 →' : '返回刚才的运行'}</Link>}</div>
    </form>
    <section className="wb-card"><div className="wb-card-head"><div><span className="wb-eyebrow">配置在哪里</span><h2>预算、额度与模型分别管理什么</h2></div></div>
      <div className="pw-budget-guide"><div><h3>本项目 · 单次费用</h3><p>限制每条需求已报告的累计美元费用，在上方设置。修改后再从暂停的运行发起重试。</p><a className="wb-text-link" href="#project-budget">定位项目预算 ↑</a></div>
        <div><h3>小组 · 月度 token 额度</h3><p>按成员、项目与整个工作区分配每月调用额度，使用 token 计量。</p><Link className="wb-text-link" to="/team">打开团队与额度 →</Link></div>
        <div><h3>工作区 · 模型与费用策略</h3><p>配置经济、标准、强能力模型，以及费用未知时是否继续。该配置影响工作区内的新规划。</p><Link className="wb-text-link" to="/settings/runtime">打开模型与运行配置 →</Link></div></div>
    </section>
  </div>
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
  return <section className="wb-card wb-requirement-card"><div className="wb-card-head"><div><span className="wb-eyebrow">需求入口</span><h2>告诉系统要完成什么</h2><p>先描述目标、范围和验收方式，系统会按项目模式自动推进，遇到缺失信息时再向你提问。</p></div></div><form className="wb-form" onSubmit={submit}><label htmlFor="project-requirement">需求描述<textarea id="project-requirement" required minLength={5} maxLength={50000} rows={6} value={value} onChange={(event) => setValue(event.target.value)} placeholder="例如：为订单服务增加批量取消接口，保留现有单笔取消行为，并补充回归检查。" /></label>{error && <ErrorNotice message={error} />}<div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy || value.trim().length < 5}>{busy ? '提交中…' : '提交需求 →'}</button></div></form></section>
}

export default function ProjectPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [policyRefresh, setPolicyRefresh] = useState(0)
  const isAdmin = user?.role !== 'member'
  const { projectId } = useParams<{ projectId: string }>()
  const [search, setSearch] = useSearchParams()
  const requestedTab = search.get('tab')
  const tab: ProjectTab = isAdmin && ['automation', 'agent', 'settings'].includes(requestedTab ?? '') ? requestedTab as ProjectTab : 'overview'
  const selectedStage = projectStage(search.get('stage')).id
  const [project, setProject] = useState<ProjectRecord | null>(null)
  const [runs, setRuns] = useState<Run[] | null>(null)
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
      const [runResult, summaryResult] = await Promise.allSettled([
        request<{ runs: Run[] }>(`/api/v2/runs?project_id=${encodeURIComponent(projectId)}`, { onUnauthorized, signal: controller.signal }),
        request<OverviewData>(`/api/v3/overview?project_id=${encodeURIComponent(projectId)}`, { onUnauthorized, signal: controller.signal }),
      ])
      if (!controller.signal.aborted) {
        if (runResult.status === 'fulfilled') { setRuns(runResult.value.runs.filter((run) => String(run.project_id) === projectId)); setRunsError(null) } else setRunsError(errorText(runResult.reason))
        if (summaryResult.status === 'fulfilled' && summaryResult.value.project_id === projectId) { setOverview(summaryResult.value); setOverviewError(null) }
        else setOverviewError(summaryResult.status === 'rejected' ? errorText(summaryResult.reason) : '项目范围与返回内容不一致，请刷新。')
      }
      busy = false
    }
    void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 15000)
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
    <div className="pw-context"><Link to="/">工程总览</Link><span>/</span><Link to="/projects">项目</Link><span>/</span><span>{project.name}</span></div>
    <PageHeader title={project.name} description={`${project.repository} · ${project.base_branch}`} actions={isAdmin ? <Link className="wb-button wb-button-secondary pw-budget-link" to={`/projects/${encodedId}?tab=settings#project-budget`}><span>单次运行预算</span><strong>${(project.budget_usd ?? 0).toFixed(2)}</strong><span>调整 →</span></Link> : <span className="wb-runtime-note">配置由项目管理员维护</span>} />
    <nav className="wb-project-tabs" aria-label="项目工作区">{(isAdmin ? [['overview', '工程闭环'], ['agent', '项目知识'], ['automation', 'Auto 与能力'], ['settings', '预算与设置']] as const : [['overview', '工程闭环']] as const).map(([key, label]) => <button key={key} aria-current={tab === key ? 'page' : undefined} className={`wb-project-tab ${tab === key ? 'is-active' : ''}`} onClick={() => setTab(key)}>{label}</button>)}</nav>
    <ProjectMode key={`${projectId}:${policyRefresh}`} projectId={String(project.id)} isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
    {tab === 'overview' && <>
      {error && <ErrorNotice message={error} />}{overviewError && <ErrorNotice message={overviewError} />}{runsError && <ErrorNotice message={runsError} />}
      {overview && <section className="ov3-summary" aria-label="本项目进展"><div><span>已接收需求</span><strong>{overview.runs}</strong><small>只统计本项目</small></div><div><span>进行中</span><strong>{overview.active_runs}</strong><small>规划、执行或验证</small></div><div className={overview.attention_runs ? 'is-attention' : ''}><span>待处理</span><strong>{overview.attention_runs}</strong><small>需要处理的具体原因见下方</small></div><div><span>已知累计费用</span><strong>${overview.known_cost_usd.toFixed(2)}</strong><small>{overview.unknown_cost_runs} 次费用未完整返回</small></div></section>}
      {(overview?.attention ?? []).length > 0 && <section className="wb-card pw-attention"><div className="ov3-section-head"><div><span className="wb-eyebrow">本项目 · 待处理</span><h2>让工作继续的下一步</h2></div></div><AttentionList items={overview!.attention!.slice(0, 3)} /></section>}
      {overview?.engineering && runs ? <ProjectLifecycle projectId={project.id} projectName={project.name} engineering={overview.engineering} runs={runs} selectedStage={selectedStage} onSelect={setStage} requirementForm={requirementForm} isAdmin={isAdmin} /> : !overviewError && <div className="wb-card ov3-loading" role="status" aria-label="正在读取本项目闭环"><span /><span /></div>}
      <details className="wb-card pw-preflight"><summary>项目准备与执行边界 · {readiness ? readiness.ready ? '可以开始' : '有配置需要处理' : '读取中'}</summary><div className="pw-preflight-body">
        <div className="wb-fact-grid"><div><span>基础分支</span><strong>{project.base_branch}</strong></div><div><span>验收检查</span><strong>{Object.keys(project.checks ?? {}).length} 条</strong></div><div><span>运行状态</span><strong>{activeRun ? '有进行中的运行' : '当前无执行任务'}</strong></div></div>
        <p className="wb-runtime-note">{project.workspace}</p>{readinessError && <ErrorNotice message={readinessError} />}
        {readiness && <div className="wb-project-readiness-list">{readiness.checks.map((check) => <div className="wb-project-readiness-row" key={check.id}><span className={`wb-readiness-dot ${check.status === 'ok' ? 'is-ready' : check.status === 'warning' ? 'is-warning' : 'is-blocked'}`} /><span><strong>{check.label}</strong><small>{check.message}</small></span></div>)}</div>}
        {isAdmin && <Link className="wb-text-link" to={`/projects/${encodedId}?tab=settings#project-checks`}>配置验收检查与执行边界 →</Link>}
      </div></details>
      <section className="wb-card pw-runs-list"><div className="wb-card-head"><div><span className="wb-eyebrow">本项目 · 需求档案</span><h2>每条需求的状态与下一步</h2></div><Link className="wb-text-link" to={`/runs?project_id=${encodedId}`}>全部运行 →</Link></div>
        {runs?.length === 0 && <EmptyState title="还没有需求记录" description="在需求澄清阶段提交这个项目的第一个目标。" />}
        {runs && runs.length > 0 && <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>需求</th><th>目前状态与原因</th><th>下一步</th></tr></thead><tbody>{runs.slice(0, 20).map((run) => { const guidance = runGuidance(run); return <tr key={String(run.id)}><td><Link className="wb-table-link" to={guidance.primaryHref}>{run.plan?.title || run.request.slice(0, 90)}</Link><small>{formatDate(run.updated_at)}</small></td><td><strong>{guidance.label}</strong><div className="pw-run-reason">{guidance.summary}</div></td><td><Link className="wb-text-link" to={guidance.primaryHref}>{guidance.primaryLabel} →</Link></td></tr> })}</tbody></table></div>}
      </section>
    </>}
    {tab === 'agent' && isAdmin && <section className="wb-card wb-agent-card"><div className="wb-card-head"><div><span className="wb-eyebrow">{project.name} · 项目知识</span><h2>维护这个项目的知识和代码上下文</h2><p>知识与索引属于当前项目。跨项目复用通过明确的能力绑定完成。</p></div></div><div className="wb-project-agent"><ProjectAgent key={String(project.id)} projectId={project.id} repository={project.repository} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /></div></section>}
    {tab === 'automation' && isAdmin && <ProjectAutomation key={String(project.id)} projectId={String(project.id)} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onPolicySaved={() => setPolicyRefresh((value) => value + 1)} />}
    {tab === 'settings' && isAdmin && <EditSettings project={project} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onSaved={setProject} />}
  </div>
}
