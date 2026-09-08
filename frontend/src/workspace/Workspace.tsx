import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { request, WorkspaceApiError } from './api'
import TaskGraph from './TaskGraph'
import ProjectAgent from './ProjectAgent'
import RunKnowledge from './RunKnowledge'
import type { AuditEvent, ConversationMessage, Plan, Project, ProviderInfo, ProviderProfile, ProvidersResponse, Run, RunStatus, TaskStatus, User } from './types'
import './workspace.css'

interface WorkspaceProps {
  user: User
  csrfToken: string
  onLogout: () => void
}

interface ProjectForm {
  name: string
  repository: string
  workspace: string
  base_branch: string
  checks: string
  auto_issues: boolean
  auto_publish: boolean
}

const statusLabels: Record<RunStatus, string> = {
  received: '已接收', planning: '规划中', needs_clarification: '待澄清', awaiting_approval: '待审批', queued: '已排队', running: '执行中', verifying: '验证中', ready_for_review: '待复核', publishing: '发布中', published: '已发布', needs_human: '需人工介入', failed: '失败', cancelled: '已取消',
}

const initialProjectForm: ProjectForm = {
  name: '', repository: '', workspace: '', base_branch: 'main', checks: '{\n  "test": ["pytest", "-q"]\n}', auto_issues: false, auto_publish: false,
}

function displayDate(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function errorMessage(error: unknown): string {
  return error instanceof WorkspaceApiError ? error.detail : error instanceof Error ? error.message : '请求失败'
}

function payloadText(payload: unknown): string {
  if (payload === undefined || payload === null) return ''
  if (typeof payload === 'string') return payload
  try { return JSON.stringify(payload, null, 2) } catch { return String(payload) }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function githubPrUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname === 'github.com' && /^\/[^/]+\/[^/]+\/pull\/\d+(?:\/)?$/.test(url.pathname)
  } catch {
    return false
  }
}

function ArtifactChecks({ value }: { value: unknown }) {
  if (!Array.isArray(value)) return <details><summary>查看检查证据</summary><pre className="wf-event-payload">{payloadText(value)}</pre></details>
  if (!value.length) return <span className="wf-muted">没有检查记录</span>
  return <table className="wf-table"><thead><tr><th>检查</th><th>结果</th><th>退出码</th></tr></thead><tbody>{value.map((item, index) => {
    const check = isRecord(item) ? item : {}
    const name = typeof check.name === 'string' ? check.name : `检查 ${index + 1}`
    const outcome = check.timeout ? '超时' : check.outcome === 'passed' || check.outcome === 'pass' || check.exit === 0 || check.status === 'passed' ? '通过' : check.exit === undefined && check.status === undefined && check.outcome === undefined ? '已记录' : typeof check.outcome === 'string' ? check.outcome : '失败'
    const exit = check.exit === undefined || check.exit === null ? '—' : String(check.exit)
    return <tr key={`${name}-${index}`}><td>{name}</td><td><span className={`wf-pill ${outcome === '通过' ? 'wf-pill-green' : outcome === '失败' || outcome === '超时' ? 'wf-pill-red' : ''}`}>{outcome}</span></td><td>{exit}</td></tr>
  })}</tbody></table>
}

function ArtifactValue({ name, value }: { name: string; value: unknown }) {
  if (value === null || value === undefined || value === '') return <span>—</span>
  if (name === 'pr_url' && githubPrUrl(value)) return <a href={value} target="_blank" rel="noreferrer">查看 GitHub PR</a>
  if (name === 'checks') return <ArtifactChecks value={value} />
  if (Array.isArray(value) || isRecord(value)) return <details><summary>查看结构化证据</summary><pre className="wf-event-payload">{payloadText(value)}</pre></details>
  return <span>{String(value)}</span>
}

const taskStatuses = new Set<TaskStatus>(['pending', 'queued', 'running', 'verified', 'completed', 'failed', 'blocked', 'cancelled'])

function taskStatusMap(value: unknown): Map<string, TaskStatus> {
  const result = new Map<string, TaskStatus>()
  if (!Array.isArray(value)) return result
  value.forEach((item) => {
    if (!item || typeof item !== 'object') return
    const id = 'id' in item ? item.id : undefined
    const status = 'status' in item ? item.status : undefined
    if (typeof id === 'string' && typeof status === 'string' && taskStatuses.has(status as TaskStatus)) result.set(id, status as TaskStatus)
  })
  return result
}

function StatusPill({ status }: { status: RunStatus }) {
  const tone = status === 'published' || status === 'ready_for_review' ? 'wf-pill-green' : status === 'failed' || status === 'needs_human' ? 'wf-pill-red' : status === 'needs_clarification' || status === 'awaiting_approval' ? 'wf-pill-amber' : ''
  return <span className={`wf-pill ${tone}`}><i className={`wf-status-dot status-${status}`} />{statusLabels[status]}</span>
}

function ProjectFormCard({ csrfToken, onCreated, onUnauthorized }: { csrfToken: string; onCreated: (project: Project) => void; onUnauthorized: () => void }) {
  const [form, setForm] = useState<ProjectForm>(initialProjectForm)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const update = (key: keyof ProjectForm, value: string | boolean) => setForm((current) => ({ ...current, [key]: value }))
  const create = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    let checks: unknown
    try { checks = JSON.parse(form.checks) } catch { setError('checks 必须是 JSON 对象，值必须是 argv 字符串数组。'); return }
    if (!checks || Array.isArray(checks) || typeof checks !== 'object' || Object.values(checks as Record<string, unknown>).some((argv) => !Array.isArray(argv) || argv.some((part) => typeof part !== 'string'))) {
      setError('checks 必须是名称到字符串数组的映射，例如 {"test":["pytest","-q"]}。'); return
    }
    setSaving(true)
    try {
      const project = await request<Project>('/api/v2/projects', { method: 'POST', csrfToken, onUnauthorized, body: { name: form.name.trim(), repository: form.repository.trim(), workspace: form.workspace.trim(), base_branch: form.base_branch.trim() || 'main', checks, auto_issues: form.auto_issues, auto_publish: form.auto_publish } })
      onCreated(project)
      setForm(initialProjectForm)
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorMessage(cause)) } finally { setSaving(false) }
  }
  return (
    <section className="wf-panel wf-section">
      <h2>创建工程</h2>
      <p className="wf-muted">工程配置由工程师明确提供，校验命令来自可信项目设置。</p>
      <form onSubmit={create}>
        <div className="wf-grid wf-grid-3">
          <div className="wf-field"><label htmlFor="project-name">名称</label><input id="project-name" required value={form.name} onChange={(event) => update('name', event.target.value)} placeholder="payments-api" /></div>
          <div className="wf-field"><label htmlFor="project-repository">仓库（owner/name）</label><input id="project-repository" required value={form.repository} onChange={(event) => update('repository', event.target.value)} placeholder="acme/payments" /></div>
          <div className="wf-field"><label htmlFor="project-workspace">工作区（服务器已有 checkout）</label><input id="project-workspace" required value={form.workspace} onChange={(event) => update('workspace', event.target.value)} placeholder="/workspaces/payments" /></div>
          <div className="wf-field"><label htmlFor="project-branch">基础分支</label><input id="project-branch" value={form.base_branch} onChange={(event) => update('base_branch', event.target.value)} /></div>
          <div className="wf-field" style={{ gridColumn: 'span 2' }}><label htmlFor="project-checks">checks（JSON：名称 → argv 数组）</label><textarea id="project-checks" required value={form.checks} onChange={(event) => update('checks', event.target.value)} rows={4} /></div>
        </div>
        <div className="wf-pills" style={{ marginTop: 12 }}>
          <label className="wf-pill"><input type="checkbox" checked={form.auto_issues} onChange={(event) => update('auto_issues', event.target.checked)} />允许带 factory-ready 标签的低风险 Issue 自动执行</label>
          <label className="wf-pill"><input type="checkbox" checked={form.auto_publish} onChange={(event) => update('auto_publish', event.target.checked)} />允许自动发布</label>
        </div>
        {error && <div className="wf-error" role="alert">{error}</div>}
        <div className="wf-form-actions"><button className="wf-button wf-button-primary" disabled={saving}>{saving ? '创建中…' : '创建工程'}</button></div>
      </form>
    </section>
  )
}

function NewRunCard({ project, csrfToken, onCreated, onUnauthorized }: { project: Project; csrfToken: string; onCreated: (run: Run) => void; onUnauthorized: () => void }) {
  const [requestText, setRequestText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null); setSubmitting(true)
    try {
      const run = await request<Run>('/api/v2/runs', { method: 'POST', csrfToken, onUnauthorized, body: { project_id: project.id, request: requestText.trim() } })
      onCreated(run); setRequestText('')
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorMessage(cause)) } finally { setSubmitting(false) }
  }
  return <section className="wf-panel wf-section"><h2>新建运行</h2><p className="wf-muted">工程：{project.name} · {project.repository}</p><form onSubmit={submit}><div className="wf-field"><label htmlFor="run-request">需求</label><textarea id="run-request" required minLength={1} value={requestText} onChange={(event) => setRequestText(event.target.value)} placeholder="描述希望完成的工程任务、边界和验收目标。" /></div>{error && <div className="wf-error" role="alert">{error}</div>}<div className="wf-form-actions"><button className="wf-button wf-button-primary" disabled={submitting || !requestText.trim()}>{submitting ? '提交中…' : '提交需求'}</button></div></form></section>
}

function ProviderCard({ providers, profiles }: { providers: ProviderInfo[]; profiles: Record<string, ProviderProfile> }) {
  return <section className="wf-panel wf-sidebar-panel"><h2 className="wf-panel-title">Provider 配置</h2>{providers.length === 0 ? <div className="wf-muted">没有已配置 provider。</div> : providers.map((provider) => <div key={provider.id} className="wf-project-meta" style={{ marginBottom: 7 }}><span className={`wf-status-dot ${provider.installed ? 'status-published' : 'status-failed'}`} />{provider.id} · {provider.installed ? '已安装' : '未安装'}{provider.detail ? ` · ${provider.detail}` : ''}</div>)}<div style={{ borderTop: '1px solid #edf1f5', marginTop: 10, paddingTop: 9 }}>{Object.entries(profiles).map(([name, profile]) => <div className="wf-rail-row" key={name}><span>{name}</span><span>{profile.provider ?? '—'}{profile.model ? ` / ${profile.model}` : ''}</span></div>)}</div></section>
}

function RunDetail({ run, project, csrfToken, onChanged, onUnauthorized, onShowProject }: { run: Run; project?: Project; csrfToken: string; onChanged: (run: Run) => void; onUnauthorized: () => void; onShowProject: () => void }) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'plan' | 'conversation' | 'events'>('plan')
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null)
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [auxError, setAuxError] = useState<string | null>(null)

  const act = async (kind: 'clarify' | 'approve' | 'cancel' | 'publish') => {
    setError(null); setBusy(kind)
    const endpoint = kind === 'clarify' ? 'clarify' : kind
    const body = kind === 'clarify' ? { answer: answer.trim() } : kind === 'approve' ? { revision: run.revision } : undefined
    try {
      const next = await request<Run>(`/api/v2/runs/${encodeURIComponent(String(run.id))}/${endpoint}`, { method: 'POST', csrfToken, onUnauthorized, body })
      onChanged(next); if (kind === 'clarify') setAnswer('')
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorMessage(cause)) } finally { setBusy(null) }
  }

  useEffect(() => {
    setMessages(null)
    setEvents(null)
    setAuxError(null)
  }, [run.id])

  useEffect(() => {
    if (tab === 'plan') return
    let cancelled = false
    setAuxError(null)
    if (tab === 'conversation') setMessages(null)
    else setEvents(null)
    const load = async () => {
      try {
        if (tab === 'conversation') {
          const result = await request<{ messages: ConversationMessage[] }>(`/api/v2/runs/${encodeURIComponent(String(run.id))}/conversation`, { onUnauthorized })
          if (!cancelled) setMessages(result.messages)
        } else {
          const collected: AuditEvent[] = []
          let after = 0
          for (let page = 0; page < 100; page += 1) {
            const result = await request<{ events: AuditEvent[]; cursor: number }>(`/api/v2/runs/${encodeURIComponent(String(run.id))}/events?after=${after}`, { onUnauthorized })
            collected.push(...result.events)
            if (!result.events.length || result.cursor <= after) break
            after = result.cursor
          }
          if (!cancelled) setEvents(collected)
        }
      } catch (cause) { if (!cancelled && !(cause instanceof WorkspaceApiError && cause.status === 401)) setAuxError(errorMessage(cause)) }
    }
    load()
    return () => { cancelled = true }
  }, [tab, run.id, run.updated_at, onUnauthorized])

  const plan: Plan | null | undefined = run.plan
  const questions = Array.from(new Set([...(plan?.questions ?? []), ...(run.triage?.questions ?? [])]))
  const canClarify = ['needs_clarification', 'needs_human', 'awaiting_approval'].includes(run.status)
  const canApprove = run.status === 'awaiting_approval' && questions.length === 0
  const currentTaskStatuses = taskStatusMap(run.tasks)
  const graphTasks = plan?.tasks?.map((task) => ({ ...task, status: currentTaskStatuses.get(task.id) ?? task.status })) ?? []
  return <>
    <div className="wf-heading"><div><h1>{plan?.title || `运行 #${run.id}`}</h1><p>{project?.name ?? `工程 ${run.project_id}`} · 更新于 {displayDate(run.updated_at)}</p></div><div className="wf-heading-actions"><button className="wf-button" onClick={onShowProject}>项目档案</button><a className="wf-button" href={`/api/v2/runs/${encodeURIComponent(String(run.id))}/export`} target="_blank" rel="noreferrer">导出 Markdown</a>{['received', 'planning', 'needs_clarification', 'awaiting_approval', 'queued', 'running', 'verifying'].includes(run.status) && <button className="wf-button wf-button-danger" disabled={busy !== null} onClick={() => act('cancel')}>{busy === 'cancel' ? '取消中…' : '取消运行'}</button>}{run.status === 'ready_for_review' && <button className="wf-button wf-button-primary" disabled={busy !== null} onClick={() => act('publish')}>{busy === 'publish' ? '发布中…' : '发布'}</button>}</div></div>
    {error && <div className="wf-error" role="alert">{error}</div>}
    <div className="wf-run-detail"><div className="wf-main-detail">
      <section className="wf-panel wf-section"><div className="wf-tabs"><button className={`wf-tab ${tab === 'plan' ? 'is-active' : ''}`} onClick={() => setTab('plan')}>计划与任务</button><button className={`wf-tab ${tab === 'conversation' ? 'is-active' : ''}`} onClick={() => setTab('conversation')}>对话</button><button className={`wf-tab ${tab === 'events' ? 'is-active' : ''}`} onClick={() => setTab('events')}>审计事件</button></div>
        {tab === 'plan' && <>{plan ? <><h2>{plan.title}</h2><div className="wf-summary">{plan.summary}</div><div style={{ marginTop: 18 }}><h3>任务依赖与状态</h3><TaskGraph tasks={graphTasks} /></div></> : <div className="wf-empty">计划尚未返回，当前状态为“{statusLabels[run.status]}”。</div>}</>}
        {tab === 'conversation' && <>{auxError && <div className="wf-error">{auxError}</div>}{messages === null && !auxError ? <div className="wf-empty">正在读取对话…</div> : messages && messages.length ? <div>{messages.map((message) => <article key={String(message.id)} style={{ borderBottom: '1px solid #eef1f5', padding: '11px 0' }}><div className="wf-muted">{message.role} · {displayDate(message.at)}</div><div className="wf-summary" style={{ marginTop: 4 }}>{message.content}</div></article>)}</div> : messages ? <div className="wf-empty">没有可展示的对话记录。</div> : null}</>}
        {tab === 'events' && <>{auxError && <div className="wf-error">{auxError}</div>}{events === null && !auxError ? <div className="wf-empty">正在读取审计事件…</div> : events && events.length ? <div>{events.map((event) => <div className="wf-event" key={event.id}><span className="wf-event-type">{event.type}</span><span className="wf-event-time">{displayDate(event.at)}</span>{event.task_id && <span className="wf-muted"> · {event.task_id}</span>}<div className="wf-event-payload">{payloadText(event.payload)}</div></div>)}</div> : events ? <div className="wf-empty">没有持久化审计事件。</div> : null}</>}
      </section>
      {canClarify && <section className="wf-panel wf-section"><h2>{questions.length ? '需要澄清' : '补充信息'}</h2>{questions.length > 0 && <ul className="wf-question-list">{questions.map((question) => <li key={question}>{question}</li>)}</ul>}<div className="wf-field" style={{ marginTop: 12 }}><label htmlFor="clarification">针对当前计划版本 {run.revision} 的补充</label><textarea id="clarification" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="提供事实、边界或选择，提交后将重新规划。" /></div><div className="wf-form-actions"><button className="wf-button wf-button-primary" disabled={!answer.trim() || busy !== null} onClick={() => act('clarify')}>{busy === 'clarify' ? '提交并重新规划…' : '提交补充并重新规划'}</button></div></section>}
      {canApprove && <section className="wf-panel wf-section"><h2>计划审批</h2><p className="wf-muted">当前计划版本 {run.revision} 没有未解决问题。批准后将进入有界 DAG 调度。</p><div className="wf-form-actions"><button className="wf-button wf-button-primary" disabled={busy !== null} onClick={() => act('approve')}>{busy === 'approve' ? '批准中…' : `批准计划版本 ${run.revision}`}</button></div></section>}
      <RunKnowledge key={String(run.id)} runId={run.id} revision={run.revision} status={run.status} artifacts={run.artifacts} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />
    </div><aside className="wf-detail-rail"><section className="wf-panel wf-rail-card"><h3>运行状态</h3><div style={{ marginBottom: 9 }}><StatusPill status={run.status} /></div><div className="wf-rail-row"><span>运行 ID</span><span>{run.id}</span></div><div className="wf-rail-row"><span>计划版本</span><span>{run.revision}</span></div><div className="wf-rail-row"><span>创建时间</span><span>{displayDate(run.created_at)}</span></div></section><section className="wf-panel wf-rail-card"><h3>分诊</h3>{run.triage ? <><div className="wf-rail-row"><span>决定</span><span>{run.triage.decision}</span></div><div className="wf-rail-row"><span>风险</span><span>{run.triage.risk}</span></div>{run.triage.reasons.map((reason) => <div className="wf-muted" key={reason} style={{ marginTop: 7 }}>{reason}</div>)}</> : <div className="wf-muted">尚未返回分诊结果</div>}</section><section className="wf-panel wf-rail-card"><h3>交付物</h3>{run.artifacts && Object.keys(run.artifacts).length ? Object.entries(run.artifacts).map(([key, value]) => <div className="wf-rail-row" key={key}><span>{key}</span><div style={{ textAlign: 'right', overflowWrap: 'anywhere' }}><ArtifactValue name={key} value={value} /></div></div>) : <div className="wf-muted">尚无交付物记录</div>}</section></aside></div>
  </>
}

export default function Workspace({ user, csrfToken, onLogout }: WorkspaceProps) {
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [providers, setProviders] = useState<ProvidersResponse | null>(null)
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [selectedProjectId, setSelectedProjectId] = useState<string | number | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | number | null>(null)
  const [showProjectForm, setShowProjectForm] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)
  const runFetchSequence = useRef(0)

  const onUnauthorized = useCallback(() => { onLogout() }, [onLogout])
  const loadProjectsAndProviders = useCallback(async () => {
    setPageError(null)
    try {
      const [projectResponse, providerResponse] = await Promise.all([
        request<{ projects: Project[] }>('/api/v2/projects', { onUnauthorized }),
        request<ProvidersResponse>('/api/v2/providers', { onUnauthorized }),
      ])
      setProjects(projectResponse.projects)
      setProviders(providerResponse)
      if (projectResponse.projects.length) setSelectedProjectId((current) => current ?? projectResponse.projects[0].id)
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) { setPageError(errorMessage(cause)); setProjects(null); setProviders(null) } }
  }, [onUnauthorized])

  const loadRuns = useCallback(async () => {
    const sequence = ++runFetchSequence.current
    try {
      const response = await request<{ runs: Run[] }>('/api/v2/runs', { onUnauthorized })
      if (sequence !== runFetchSequence.current) return
      setRuns(response.runs)
      setSyncError(null)
      setSelectedRunId((current) => current && response.runs.some((run) => String(run.id) === String(current)) ? current : null)
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401) && sequence === runFetchSequence.current) { setSyncError(errorMessage(cause)); setRuns(null) } }
  }, [onUnauthorized])

  const loadSelectedRun = useCallback(async (id: string | number) => {
    const sequence = ++runFetchSequence.current
    try {
      const next = await request<Run>(`/api/v2/runs/${encodeURIComponent(String(id))}`, { onUnauthorized })
      if (sequence !== runFetchSequence.current) return
      setRuns((current) => current?.map((run) => String(run.id) === String(next.id) ? next : run) ?? [next])
      setSyncError(null)
    } catch (cause) {
      if (!(cause instanceof WorkspaceApiError && cause.status === 401) && sequence === runFetchSequence.current) {
        setSelectedRunId(null)
        setSyncError(`运行 ${id} 详情同步失败：${errorMessage(cause)}`)
      }
    }
  }, [onUnauthorized])

  useEffect(() => { loadProjectsAndProviders(); loadRuns() }, [loadProjectsAndProviders, loadRuns])
  useEffect(() => { const timer = window.setInterval(loadRuns, 4000); return () => window.clearInterval(timer) }, [loadRuns])
  useEffect(() => {
    if (selectedRunId === null) return undefined
    loadSelectedRun(selectedRunId)
    const timer = window.setInterval(() => loadSelectedRun(selectedRunId), 3500)
    return () => { window.clearInterval(timer); runFetchSequence.current += 1 }
  }, [loadSelectedRun, selectedRunId])

  const selectedProject = projects?.find((project) => String(project.id) === String(selectedProjectId))
  const selectedRun = runs?.find((run) => String(run.id) === String(selectedRunId))
  const visibleRuns = useMemo(() => selectedProjectId === null ? runs ?? [] : (runs ?? []).filter((run) => String(run.project_id) === String(selectedProjectId)), [runs, selectedProjectId])

  const handleProjectCreated = (project: Project) => { setProjects((current) => current ? [...current, project] : [project]); setSelectedProjectId(project.id); setShowProjectForm(false) }
  const handleRunCreated = (run: Run) => { setRuns((current) => current ? [run, ...current] : [run]); setSelectedRunId(run.id) }
  const handleRunChanged = (next: Run) => { setRuns((current) => current?.map((run) => String(run.id) === String(next.id) ? next : run) ?? [next]) }

  return <div className="wf-shell"><header className="wf-topbar"><div className="wf-brand">无人工厂 · 工程工作台</div><nav className="wf-topbar-nav"><a href="/" className="wf-topbar-link">工作台</a><a href="/control-room" className="wf-topbar-link">控制室</a><a href="/admin" className="wf-topbar-link">旧版管理台</a></nav><div className="wf-topbar-meta"><span>已登录：{user.username}</span><button className="wf-button" onClick={onLogout}>退出登录</button></div></header><div className="wf-layout"><aside className="wf-sidebar"><section className="wf-panel wf-sidebar-panel"><div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}><h2 className="wf-panel-title">工程</h2><button className="wf-button wf-button-quiet" style={{ padding: 2, fontSize: 18 }} aria-label="创建工程" onClick={() => setShowProjectForm((value) => !value)}>＋</button></div>{projects === null ? <div className="wf-muted">正在读取工程…</div> : projects.length === 0 ? <div className="wf-empty">还没有工程。点击 ＋ 创建一个。</div> : projects.map((project) => <button className={`wf-project-item ${String(project.id) === String(selectedProjectId) ? 'is-active' : ''}`} key={String(project.id)} onClick={() => { setSelectedProjectId(project.id); setSelectedRunId(null) }}><span className="wf-project-name">{project.name}</span><span className="wf-project-meta">{project.repository}</span></button>)}</section><section className="wf-panel wf-sidebar-panel"><h2 className="wf-panel-title">需求 / 运行</h2>{runs === null ? <div className="wf-muted">正在同步需求和运行…</div> : visibleRuns.length === 0 ? <div className="wf-empty">这个工程还没有需求。提交一条需求开始规划。</div> : visibleRuns.map((run) => <button className={`wf-run-item ${String(run.id) === String(selectedRunId) ? 'is-active' : ''}`} key={String(run.id)} onClick={() => setSelectedRunId(run.id)}><span className="wf-run-name"><i className={`wf-status-dot status-${run.status}`} />{run.plan?.title || `需求 #${run.id}`}</span><span className="wf-run-meta">{run.request.slice(0, 68)}{run.request.length > 68 ? '…' : ''}</span><span className="wf-run-meta">{statusLabels[run.status]} · {displayDate(run.updated_at)}</span></button>)}</section>{providers && <ProviderCard providers={providers.providers} profiles={providers.profiles} />}</aside><main className="wf-content">{pageError && <div className="wf-error" role="alert">{pageError}</div>}{syncError && <div className="wf-error" role="alert">运行列表同步失败：{syncError}。已清除本次列表数据。</div>}{showProjectForm && <ProjectFormCard csrfToken={csrfToken} onCreated={handleProjectCreated} onUnauthorized={onUnauthorized} />}{selectedRun ? <RunDetail run={selectedRun} project={selectedProject} csrfToken={csrfToken} onChanged={handleRunChanged} onUnauthorized={onUnauthorized} onShowProject={() => setSelectedRunId(null)} /> : selectedProject ? <><div className="wf-heading"><div><h1>{selectedProject.name}</h1><p>{selectedProject.repository} · {selectedProject.workspace}</p></div><button className="wf-button wf-button-primary" onClick={() => setShowProjectForm(false)}>项目档案</button></div><NewRunCard project={selectedProject} csrfToken={csrfToken} onCreated={handleRunCreated} onUnauthorized={onUnauthorized} /><ProjectAgent key={String(selectedProject.id)} projectId={selectedProject.id} repository={selectedProject.repository} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /><section className="wf-panel wf-section"><h2>工程设置</h2><div className="wf-stat-grid"><div className="wf-stat"><div className="wf-stat-label">基础分支</div><div className="wf-stat-value" style={{ fontSize: 14 }}>{selectedProject.base_branch}</div></div><div className="wf-stat"><div className="wf-stat-label">可信检查</div><div className="wf-stat-value">{Object.keys(selectedProject.checks).length}</div></div><div className="wf-stat"><div className="wf-stat-label">Issue 自动执行</div><div className="wf-stat-value" style={{ fontSize: 14 }}>{selectedProject.auto_issues ? '开启' : '关闭'}</div></div><div className="wf-stat"><div className="wf-stat-label">自动发布</div><div className="wf-stat-value" style={{ fontSize: 14 }}>{selectedProject.auto_publish ? '开启' : '关闭'}</div></div></div><table className="wf-table" style={{ marginTop: 15 }}><thead><tr><th>检查名称</th><th>argv</th></tr></thead><tbody>{Object.entries(selectedProject.checks).map(([name, argv]) => <tr key={name}><td>{name}</td><td><code>{argv.join(' ')}</code></td></tr>)}</tbody></table></section></> : <div className="wf-panel wf-section"><div className="wf-empty">选择一个工程开始，或从左侧 ＋ 创建工程。</div></div>}</main></div></div>
}
