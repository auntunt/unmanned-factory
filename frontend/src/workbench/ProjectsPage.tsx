import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import type { Project } from '../workspace/types'
import { EmptyState, ErrorNotice, PageHeader, formatDate } from './ui'
import type { PageProps } from './ui'

export interface ProjectRecord extends Project {
  revision?: number
  budget_usd?: number
  managed_workspace?: boolean
}

export interface ProjectCandidate { id: string; name: string; repository: string; base_branch: string; registered: boolean; project_id?: string | number | null }

export function availableProjectCandidates(candidates: ProjectCandidate[]): ProjectCandidate[] { return candidates.filter((candidate) => !candidate.registered) }

interface ProjectDraft {
  name: string
  candidate_id: string
  budget_usd: string
}

const blankDraft: ProjectDraft = { name: '', candidate_id: '', budget_usd: '10' }

function errorText(error: unknown): string {
  if (error instanceof WorkspaceApiError) return error.detail
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

function ProjectForm({ csrfToken, onUnauthorized, onCreated, onCancel }: PageProps & { onCreated: (project: ProjectRecord, warning?: string) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<ProjectDraft>(blankDraft)
  const [mode, setMode] = useState<'workspace' | 'connect'>('workspace')
  const [candidates, setCandidates] = useState<ProjectCandidate[] | null>(null)
  const [candidateError, setCandidateError] = useState<string | null>(null)
  const [rootAvailable, setRootAvailable] = useState(true)
  const [refresh, setRefresh] = useState(0)
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])
  useEffect(() => { if (mode !== 'connect') return; const controller = new AbortController(); setCandidates(null); setCandidateError(null); void request<{ candidates: ProjectCandidate[]; root_available: boolean }>('/api/v2/project-candidates', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) { setCandidates(payload.candidates); setRootAvailable(payload.root_available) } }).catch((cause) => { if (!controller.signal.aborted) setCandidateError(errorText(cause)) }); return () => controller.abort() }, [onUnauthorized, refresh, mode])
  const update = <K extends keyof ProjectDraft>(key: K, value: ProjectDraft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    if (busy) return
    if (mode === 'workspace' && !draft.name.trim()) { setError('请填写项目名称。'); return }
    const budget = Number(draft.budget_usd)
    if (!Number.isFinite(budget) || budget <= 0) { setError('预算需要是大于 0 的数字。'); return }
    if (mode === 'connect' && !candidates?.some((item) => item.id === draft.candidate_id && !item.registered)) { setError('请从最新列表中选择工程。'); return }
    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController(); controllerRef.current = controller
    try {
      const project = mode === 'workspace'
        ? await request<ProjectRecord>('/api/v2/projects/create-workspace', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { name: draft.name.trim(), budget_usd: budget, idempotency_key: idempotencyKey } })
        : await request<ProjectRecord>('/api/v2/projects/connect', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { candidate_id: draft.candidate_id, name: draft.name.trim() || undefined, budget_usd: budget } })
      if (controller.signal.aborted) return
      onCreated(project); setDraft(blankDraft)
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  return <section className="wb-card wb-create-card" aria-labelledby="create-project-title">
    <div className="wb-card-head"><div><span className="wb-eyebrow">登记项目</span><h2 id="create-project-title">{mode === 'workspace' ? '新建工作区' : '连接已有工程'}</h2><p>先创建工作区，稍后再用口语描述需求。现在不需要准备代码或验收检查。</p></div><button className="wb-icon-button wb-close-button" type="button" onClick={onCancel} aria-label="关闭">×</button></div>
    <form className="wb-form" onSubmit={submit}>
      <div className="agent-mode-switch" role="tablist"><button type="button" className={mode === 'workspace' ? 'is-active' : ''} onClick={() => setMode('workspace')}>新建工作区</button><button type="button" className={mode === 'connect' ? 'is-active' : ''} onClick={() => setMode('connect')}>连接已有工程</button></div>
      <div className="wb-form-grid wb-form-grid-two">
        {mode === 'connect' && <label className="wb-span-two">选择工程，系统自动连接<select required value={draft.candidate_id} onChange={(event) => { const candidate = candidates?.find((item) => item.id === event.target.value); update('candidate_id', event.target.value); if (candidate) update('name', candidate.name) }} disabled={!candidates || availableProjectCandidates(candidates).length === 0}><option value="">{candidates ? availableProjectCandidates(candidates).length ? '请选择工程' : '没有发现可登记的工程' : '正在发现工程…'}</option>{candidates && availableProjectCandidates(candidates).map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}</select><small>{rootAvailable ? '工程位置由服务器维护。' : '服务器工程目录当前不可用，请稍后刷新。'}</small></label>}
        <label>项目名称<input required={mode === 'workspace'} maxLength={120} value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder={mode === 'workspace' ? '例如：我的转换器项目' : '留空使用工程名称'} /></label>
        <label>费用停止阈值（美元）<input type="number" min="0.01" max="1000" step="0.01" value={draft.budget_usd} onChange={(event) => update('budget_usd', event.target.value)} /><small>这是已上报费用的停止阈值，不保证供应商侧美元硬上限；未知费用策略在运行配置中设置。</small></label>
      </div>
      {mode === 'connect' && <><button type="button" className="wb-button wb-button-secondary" onClick={() => setRefresh((value) => value + 1)} disabled={!candidates && !candidateError}>刷新工程列表</button>{candidateError && <ErrorNotice message={`${candidateError} 可重试发现工程。`} />}</>}{error && <ErrorNotice message={error} />}
      <div className="wb-form-actions"><button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button><button className="wb-button wb-button-primary" disabled={busy || (mode === 'workspace' && !draft.name.trim()) || (mode === 'connect' && !candidates?.some((item) => item.id === draft.candidate_id && !item.registered))}>{busy ? '准备中…' : mode === 'workspace' ? '创建工作区' : '连接工程'}</button></div>
    </form>
  </section>
}

export default function ProjectsPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const isAdmin = user?.role !== 'member'
  const [projects, setProjects] = useState<ProjectRecord[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)

  useEffect(() => {
    const controller = new AbortController(); setProjects(null); setError(null); setNotice(null)
    void request<{ projects: ProjectRecord[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setProjects(payload.projects) }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized])

  return <div className="wb-page">
    <PageHeader title="项目" description="查看工程上下文和需求入口。项目设置由管理员维护。" actions={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen((value) => !value)}>{createOpen ? '关闭新建' : '新建工作区'} <span aria-hidden="true">＋</span></button> : undefined} />
    {createOpen && isAdmin && <ProjectForm csrfToken={csrfToken} onUnauthorized={onUnauthorized} onCancel={() => setCreateOpen(false)} onCreated={(project, warning) => { setProjects((current) => current ? [project, ...current] : [project]); setCreateOpen(false); if (warning) setNotice(warning); else void navigate(`/projects/${encodeURIComponent(String(project.id))}`) }} />}
    {error && <ErrorNotice message={error} />}{notice && <div className="wb-notice" role="status">{notice}</div>}
    {projects === null && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {projects && projects.length === 0 && <div className="wb-card"><EmptyState title="还没有项目" description={isAdmin ? '填写名称即可创建工作区，之后再描述具体需求。' : '管理员登记项目后，你可以在已分配的项目中提交需求。'} action={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen(true)}>创建第一个工作区</button> : undefined} /></div>}
    {projects && projects.length > 0 && <div className="wb-project-list">{projects.map((project) => { const meta = project as ProjectRecord & { updated_at?: string; created_at?: string }; return <Link className="wb-project-card" to={`/projects/${encodeURIComponent(String(project.id))}`} key={String(project.id)}><div className="wb-project-card-main"><span className="wb-project-glyph">{project.name.slice(0, 1).toUpperCase()}</span><div><h2>{project.name}</h2><p>{project.managed_workspace ? '系统管理的工作区' : project.repository}</p></div></div><div className="wb-project-card-meta"><span><b>分支</b>{project.base_branch}</span><span><b>检查</b>{Object.keys(project.checks ?? {}).length} 条</span><span><b>更新</b>{formatDate(meta.updated_at ?? meta.created_at)}</span><span className="wb-row-arrow">→</span></div></Link>})}</div>}
  </div>
}
