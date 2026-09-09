import type { Helper } from './ProjectKnowledge'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

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

export interface ProjectImportSummary {
  filename: string
  file_count: number
  warnings: string[]
  manifests: string[]
  baseline_status: 'not_run'
}

export function validateProjectZip(file: Pick<File, 'name' | 'size'> | null): string | null {
  if (!file) return '请选择项目 ZIP 压缩包。'
  if (!file.name.toLowerCase().endsWith('.zip')) return '项目文件需要是 ZIP 格式。'
  if (file.size > 20 * 1024 * 1024) return '压缩包不能超过 20 MiB。'
  if (file.size === 0) return '压缩包为空，请重新选择。'
  return null
}

export function projectImportNotice(summary: ProjectImportSummary): string {
  return `已导入 ${summary.filename}，共 ${summary.file_count} 个文件。${summary.manifests.length ? `识别到 ${summary.manifests.length} 个项目配置。` : ''}尚未运行项目；进入项目后可让助手检查启动方式、建立运行基线。${summary.warnings.length ? ` 提醒：${summary.warnings.join('；')}` : ''}`
}

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
  const [helpers, setHelpers] = useState<Helper[]>([])
  const [helperId, setHelperId] = useState('')
  useEffect(() => { const controller = new AbortController(); void request<{ agents: Helper[] }>('/api/v4/agents', { onUnauthorized, signal: controller.signal }).then((value) => setHelpers(value.agents)).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) }); return () => controller.abort() }, [onUnauthorized])
  const [mode, setMode] = useState<'workspace' | 'connect' | 'zip'>('workspace')
  const [archive, setArchive] = useState<File | null>(null)
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
    if (mode !== 'connect' && !draft.name.trim()) { setError('请填写项目名称。'); return }
    if (mode === 'zip') { const problem = validateProjectZip(archive); if (problem) { setError(problem); return } }
    const budget = Number(draft.budget_usd)
    if (!Number.isFinite(budget) || budget <= 0) { setError('预算需要是大于 0 的数字。'); return }
    if (mode === 'connect' && !candidates?.some((item) => item.id === draft.candidate_id && !item.registered)) { setError('请从最新列表中选择工程。'); return }
    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController(); controllerRef.current = controller
    try {
      if (mode === 'zip' && archive) {
        const body = new FormData(); body.append('file', archive); body.append('name', draft.name.trim()); body.append('idempotency_key', idempotencyKey); body.append('budget_usd', String(budget)); if (helperId) body.append('agent_id', helperId)
        const result = await request<{ project: ProjectRecord; import_summary: ProjectImportSummary }>('/api/v2/projects/import-zip', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body })
        if (!controller.signal.aborted) onCreated(result.project, projectImportNotice(result.import_summary))
        return
      }
      const project = mode === 'workspace'
        ? await request<ProjectRecord>('/api/v2/projects/create-workspace', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { name: draft.name.trim(), budget_usd: budget, idempotency_key: idempotencyKey, agent_id: helperId || null } })
        : await request<ProjectRecord>('/api/v2/projects/connect', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { candidate_id: draft.candidate_id, name: draft.name.trim() || undefined, budget_usd: budget, agent_id: helperId || null } })
      if (controller.signal.aborted) return
      onCreated(project); setDraft(blankDraft)
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  return <section className="wb-card wb-create-card" aria-labelledby="create-project-title">
    <div className="wb-card-head"><div><span className="wb-eyebrow">登记项目</span><h2 id="create-project-title">{mode === 'workspace' ? '新建工作区' : mode === 'zip' ? '上传项目压缩包' : '连接已有工程'}</h2><p>创建工作区或上传已有项目，再用口语交代开发和维护需求。</p></div><button className="wb-icon-button wb-close-button" type="button" onClick={onCancel} aria-label="关闭">×</button></div>
    <form className="wb-form" onSubmit={submit}>
      <label>选择智能体帮助<select value={helperId} onChange={(event) => setHelperId(event.target.value)}><option value="">使用平台通用助手</option>{helpers.map((helper) => <option key={helper.id} value={helper.id}>{helper.name} · v{helper.active_version}</option>)}</select><small>{helpers.find((helper) => helper.id === helperId)?.purpose || '选择相应职能体，把它的 Skill 与工作方法带入项目；后续可在项目知识中维护。'}</small></label>
      <label>工程来源<select disabled={busy} value={mode} onChange={(event) => { setMode(event.target.value as typeof mode); setError(null) }}><option value="workspace">新建空白工作区</option><option value="zip">上传项目 ZIP</option><option value="connect">连接服务器上的已有工程</option></select></label>
      {mode === 'zip' && <label>项目压缩包<input type="file" accept=".zip,application/zip" disabled={busy} onChange={(event) => { const file = event.target.files?.[0] || null; setArchive(file); setError(file ? validateProjectZip(file) : null); if (file && !draft.name.trim()) update('name', file.name.replace(/\.zip$/i, '').slice(0, 120)) }} /><small>最多 20 MiB。导入代码、说明和样例，保留原始材料；启动检查将在后续任务中进行。</small></label>}

      <div className="wb-form-grid wb-form-grid-two">
        {mode === 'connect' && <label className="wb-span-two">选择工程，系统自动连接<select required value={draft.candidate_id} onChange={(event) => { const candidate = candidates?.find((item) => item.id === event.target.value); update('candidate_id', event.target.value); if (candidate) update('name', candidate.name) }} disabled={!candidates || availableProjectCandidates(candidates).length === 0}><option value="">{candidates ? availableProjectCandidates(candidates).length ? '请选择工程' : '没有发现可登记的工程' : '正在发现工程…'}</option>{candidates && availableProjectCandidates(candidates).map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}</select><small>{rootAvailable ? '工程位置由服务器维护。' : '服务器工程目录当前不可用，请稍后刷新。'}</small></label>}
        <label>项目名称<input required={mode !== 'connect'} maxLength={120} value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder={mode !== 'connect' ? '例如：我的转换器项目' : '留空使用工程名称'} /></label>
        <p>计费与 token 额度由中转站统一管理。</p>
      </div>
      {mode === 'connect' && <><button type="button" className="wb-button wb-button-secondary" onClick={() => setRefresh((value) => value + 1)} disabled={!candidates && !candidateError}>刷新工程列表</button>{candidateError && <ErrorNotice message={`${candidateError} 可重试发现工程。`} />}</>}{error && <ErrorNotice message={error} />}
      <div className="wb-form-actions"><button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button><button className="wb-button wb-button-primary" disabled={busy || (mode === 'zip' && Boolean(validateProjectZip(archive))) || (mode !== 'connect' && !draft.name.trim()) || (mode === 'connect' && !candidates?.some((item) => item.id === draft.candidate_id && !item.registered))}>{busy ? mode === 'zip' ? '正在上传并识别项目…' : '准备中…' : mode === 'workspace' ? '创建工作区' : mode === 'zip' ? '导入项目' : '连接工程'}</button></div>
    </form>
  </section>
}

export default function ProjectsPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const isAdmin = user?.role !== 'member'
  const [projects, setProjects] = useState<ProjectRecord[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [importedProjectId, setImportedProjectId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(() => searchParams.get('create') === '1')

  useEffect(() => {
    const controller = new AbortController(); setProjects(null); setError(null); setNotice(null)
    void request<{ projects: ProjectRecord[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setProjects(payload.projects) }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized])

  return <div className="wb-page">
    <PageHeader title="项目" description="查看工程上下文和需求入口。项目设置由管理员维护。" actions={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen((value) => !value)}>{createOpen ? '关闭新建' : '新建工作区'} <span aria-hidden="true">＋</span></button> : undefined} />
    {createOpen && isAdmin && <ProjectForm csrfToken={csrfToken} onUnauthorized={onUnauthorized} onCancel={() => setCreateOpen(false)} onCreated={(project, warning) => { setProjects((current) => current ? [project, ...current] : [project]); setCreateOpen(false); if (warning) { setNotice(warning); setImportedProjectId(String(project.id)) } else void navigate(`/projects/${encodeURIComponent(String(project.id))}`) }} />}
    {error && <ErrorNotice message={error} />}{notice && <div className="wb-notice" role="status">{notice}{importedProjectId && <p><Link className="wb-button wb-button-primary" to={`/projects/${encodeURIComponent(importedProjectId)}`}>进入项目，开始维护 →</Link></p>}</div>}
    {projects === null && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {projects && projects.length === 0 && <div className="wb-card"><EmptyState title="还没有项目" description={isAdmin ? '填写名称即可创建工作区，之后再描述具体需求。' : '管理员登记项目后，你可以在已分配的项目中提交需求。'} action={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen(true)}>创建第一个工作区</button> : undefined} /></div>}
    {projects && projects.length > 0 && <div className="wb-project-list">{projects.map((project) => { const meta = project as ProjectRecord & { updated_at?: string; created_at?: string }; return <Link className="wb-project-card" to={`/projects/${encodeURIComponent(String(project.id))}`} key={String(project.id)}><div className="wb-project-card-main"><span className="wb-project-glyph">{project.name.slice(0, 1).toUpperCase()}</span><div><h2>{project.name}</h2><p>{project.managed_workspace ? '系统管理的工作区' : project.repository}</p></div></div><div className="wb-project-card-meta"><span><b>分支</b>{project.base_branch}</span><span><b>检查</b>{Object.keys(project.checks ?? {}).length} 条</span><span><b>更新</b>{formatDate(meta.updated_at ?? meta.created_at)}</span><span className="wb-row-arrow">→</span></div></Link>})}</div>}
  </div>
}
