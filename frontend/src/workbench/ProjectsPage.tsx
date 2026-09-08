import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import type { Project } from '../workspace/types'
import ChecksEditor, { validateChecks, type ChecksMap } from './ChecksEditor'
import { EmptyState, ErrorNotice, PageHeader, formatDate } from './ui'
import type { PageProps } from './ui'

export interface ProjectRecord extends Project {
  revision?: number
  budget_usd?: number
}

interface ProjectDraft {
  name: string
  repository: string
  workspace: string
  base_branch: string
  checks: ChecksMap
  auto_issues: boolean
  auto_publish: boolean
  budget_usd: string
  autonomous: boolean
}

const blankDraft: ProjectDraft = { name: '', repository: '', workspace: '', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false, budget_usd: '10', autonomous: true }

function errorText(error: unknown): string {
  if (error instanceof WorkspaceApiError) return error.detail
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

function ProjectForm({ csrfToken, onUnauthorized, onCreated, onCancel }: PageProps & { onCreated: (project: ProjectRecord, warning?: string) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<ProjectDraft>(blankDraft)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])
  const update = <K extends keyof ProjectDraft>(key: K, value: ProjectDraft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    const checksError = validateChecks(draft.checks)
    if (checksError) { setError(checksError); return }
    const budget = Number(draft.budget_usd)
    if (!Number.isFinite(budget) || budget <= 0) { setError('预算需要是大于 0 的数字。'); return }
    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController(); controllerRef.current = controller
    try {
      const project = await request<ProjectRecord>('/api/v2/projects', { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body: { name: draft.name.trim(), repository: draft.repository.trim(), workspace: draft.workspace.trim(), base_branch: draft.base_branch.trim() || 'main', checks: draft.checks, auto_issues: draft.auto_issues, auto_publish: draft.auto_publish, budget_usd: budget } })
      let warning: string | undefined
      if (draft.autonomous) {
        try {
          await request(`/api/v3/projects/${encodeURIComponent(String(project.id))}/policy`, { method: 'PUT', csrfToken, onUnauthorized, signal: controller.signal, body: { revision: 0, mode: 'autonomous', max_risk: 'medium', max_attempts: 2, auto_escalate: true, resume_on_restart: true } })
        } catch (cause) {
          if (controller.signal.aborted) return
          warning = `项目“${project.name}”已创建，但自主执行策略未保存：${errorText(cause)} 请进入项目的“自主运行”配置。`
        }
      }
      if (controller.signal.aborted) return
      onCreated(project, warning); setDraft(blankDraft)
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)) } finally { if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false) }
  }
  return <section className="wb-card wb-create-card" aria-labelledby="create-project-title">
    <div className="wb-card-head"><div><span className="wb-eyebrow">登记项目</span><h2 id="create-project-title">连接一个已有工程</h2><p>项目必须指向服务器上已经存在的 Git 工作区。</p></div><button className="wb-icon-button wb-close-button" type="button" onClick={onCancel} aria-label="关闭">×</button></div>
    <form className="wb-form" onSubmit={submit}>
      <div className="wb-form-grid wb-form-grid-two">
        <label>项目名称<input required maxLength={120} value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder="支付服务" /></label>
        <label>仓库（owner/name）<input required pattern="[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*" value={draft.repository} onChange={(event) => update('repository', event.target.value)} placeholder="acme/payments" /></label>
        <label className="wb-span-two">服务器工作区<input required value={draft.workspace} onChange={(event) => update('workspace', event.target.value)} placeholder="/srv/workspaces/payments" /><small>工作区和仓库注册后不可在这里修改。</small></label>
        <label>基础分支<input required value={draft.base_branch} onChange={(event) => update('base_branch', event.target.value)} /></label>
        <label>费用停止阈值（美元）<input type="number" min="0.01" max="1000" step="0.01" value={draft.budget_usd} onChange={(event) => update('budget_usd', event.target.value)} /><small>这是已上报费用的停止阈值，不保证供应商侧美元硬上限；未知费用策略在运行配置中设置。</small></label>
      </div>
      <details className="wb-advanced"><summary>检查与自动化设置</summary><div className="wb-advanced-body"><ChecksEditor value={draft.checks} onChange={(checks) => update('checks', checks)} /><div className="wb-check-options"><label className="wb-checkbox"><input type="checkbox" checked={draft.autonomous} onChange={(event) => update('autonomous', event.target.checked)} />需求明确后自主执行</label><label className="wb-checkbox"><input type="checkbox" checked={draft.auto_issues} onChange={(event) => update('auto_issues', event.target.checked)} />允许带 factory-ready 标签的问题自动执行</label><label className="wb-checkbox"><input type="checkbox" checked={draft.auto_publish} onChange={(event) => update('auto_publish', event.target.checked)} />允许通过验证后自动交付</label></div></div></details>
      {error && <ErrorNotice message={error} />}
      <div className="wb-form-actions"><button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button><button className="wb-button wb-button-primary" disabled={busy}>{busy ? '登记中…' : '登记项目'}</button></div>
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
    <PageHeader title="项目" description="查看工程上下文和需求入口。项目设置由管理员维护。" actions={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen((value) => !value)}>{createOpen ? '关闭登记' : '登记项目'} <span aria-hidden="true">＋</span></button> : undefined} />
    {createOpen && isAdmin && <ProjectForm csrfToken={csrfToken} onUnauthorized={onUnauthorized} onCancel={() => setCreateOpen(false)} onCreated={(project, warning) => { setProjects((current) => current ? [project, ...current] : [project]); setCreateOpen(false); if (warning) setNotice(warning); else void navigate(`/projects/${encodeURIComponent(String(project.id))}`) }} />}
    {error && <ErrorNotice message={error} />}{notice && <div className="wb-notice" role="status">{notice}</div>}
    {projects === null && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {projects && projects.length === 0 && <div className="wb-card"><EmptyState title="还没有项目" description={isAdmin ? '登记一个已经存在的 Git 工作区，开始接收工程需求。' : '管理员登记项目后，你可以在已分配的项目中提交需求。'} action={isAdmin ? <button className="wb-button wb-button-primary" onClick={() => setCreateOpen(true)}>登记第一个项目</button> : undefined} /></div>}
    {projects && projects.length > 0 && <div className="wb-project-list">{projects.map((project) => { const meta = project as ProjectRecord & { updated_at?: string; created_at?: string }; return <Link className="wb-project-card" to={`/projects/${encodeURIComponent(String(project.id))}`} key={String(project.id)}><div className="wb-project-card-main"><span className="wb-project-glyph">{project.name.slice(0, 1).toUpperCase()}</span><div><h2>{project.name}</h2><p>{project.repository}</p></div></div><div className="wb-project-card-meta"><span><b>分支</b>{project.base_branch}</span><span><b>检查</b>{Object.keys(project.checks ?? {}).length} 条</span><span><b>更新</b>{formatDate(meta.updated_at ?? meta.created_at)}</span><span className="wb-row-arrow">→</span></div></Link>})}</div>}
  </div>
}
