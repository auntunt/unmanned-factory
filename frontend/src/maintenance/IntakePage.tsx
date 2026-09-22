import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText, formatDate } from '../workbench/ui'
import type { PageProps } from '../workbench/ui'
import { maintenanceApi } from './api'
import type { IntakeSource, Receipt, RepoView, Requirement, RequirementStatus } from './types'

const RECEIPT_STATUS_LABEL: Record<RequirementStatus, string> = {
  dispatched: '已派发执行', pending_dispatch: '已接收，待派发', dispatch_failed: '派发失败',
}

// --- 主动提交 ---

function ManualSubmit({ csrfToken, onUnauthorized, projects, onSubmitted }: PageProps & {
  projects: RepoView[]
  onSubmitted: () => void
}) {
  const [projectId, setProjectId] = useState('')
  const [content, setContent] = useState('')
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<Receipt | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !projectId || !content.trim()) return
    setBusy(true); setError(null)
    try {
      const result = await maintenanceApi.submitRequirement(
        { project_id: projectId, content: content.trim(), idempotency_key: idempotencyKey },
        { csrfToken, onUnauthorized },
      )
      setReceipt(result)
      setContent('')
      setIdempotencyKey(crypto.randomUUID())
      onSubmitted()
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="wb-card" aria-labelledby="manual-intake-title">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">主动提交</span><h2 id="manual-intake-title">提交一个需求</h2></div>
      </div>
      <form className="wb-form" onSubmit={submit} data-testid="manual-intake-form">
        <label>
          项目
          <select required value={projectId} onChange={e => setProjectId(e.target.value)}>
            <option value="">请选择已接入的代码库</option>
            {projects.map(p => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
          </select>
        </label>
        <label>
          需要处理什么？
          <textarea required rows={5} value={content} onChange={e => setContent(e.target.value)} placeholder="简短描述需要维护的问题或诉求" />
        </label>
        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button className="wb-button wb-button-primary" disabled={busy || !projectId || !content.trim()}>{busy ? '提交中…' : '提交需求'}</button>
        </div>
      </form>
      {receipt && (
        <div className="wb-notice" role="status" data-testid="manual-receipt">
          <p>需求编号 <code>{receipt.requirement_id}</code>　状态：{RECEIPT_STATUS_LABEL[receipt.status]}</p>
          {receipt.duplicate && <p>这是一次重复提交，已返回原有记录。</p>}
          <p>已接收 ≠ 已执行。{receipt.message}</p>
          {receipt.task_id && <p><Link to={`/maintenance/${encodeURIComponent(receipt.task_id)}`}>查看任务</Link></p>}
        </div>
      )}
    </section>
  )
}

// --- 自动提交说明 ---

function AutomaticIntakeInfo() {
  return (
    <section className="wb-card" aria-labelledby="auto-intake-title">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">自动提交</span><h2 id="auto-intake-title">机器来源接入</h2></div>
      </div>
      <ul className="ms-side-list">
        <li>携带 <code>Authorization: Bearer &lt;接入令牌&gt;</code>，POST 到 <code>/api/v2/maintenance/intake</code></li>
        <li>请求体字段：<code>project_id</code>、<code>content</code>、<code>external_id</code>（可选 <code>title</code>、<code>attachments</code>）</li>
        <li>幂等键 = 来源 + 项目 + external_id；同键同内容返回原记录，同键不同内容拒绝（409）</li>
        <li>「已接收」不等于「已执行」；来源是否自动派发执行由管理员在接入来源里单独配置</li>
        <li>外部输入不能自行声明管理员权限：来源名与权限完全取自令牌，请求体中的来源/角色字段一律忽略</li>
      </ul>
      <pre className="ms-curl">{`curl -X POST https://<host>/api/v2/maintenance/intake \\
  -H "Authorization: Bearer <令牌>" \\
  -H "Content-Type: application/json" \\
  -d '{"project_id": "<项目ID>", "content": "<需求内容>", "external_id": "<外部事件标识>"}'`}</pre>
    </section>
  )
}

// --- 接入来源管理（管理员） ---

function IntakeSourcesAdmin({ csrfToken, onUnauthorized, projects }: PageProps & { projects: RepoView[] }) {
  const [sources, setSources] = useState<IntakeSource[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [selectedProjects, setSelectedProjects] = useState<string[]>([])
  const [autoDispatch, setAutoDispatch] = useState(false)
  const [busy, setBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [newToken, setNewToken] = useState<{ id: string; token: string } | null>(null)
  const [copied, setCopied] = useState(false)

  const load = useCallback(() => {
    maintenanceApi.intakeSources({ onUnauthorized })
      .then(result => setSources(result.sources))
      .catch(cause => { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) })
  }, [onUnauthorized])
  useEffect(() => { load() }, [load])

  const toggleProject = (id: string) => setSelectedProjects(current => current.includes(id) ? current.filter(p => p !== id) : [...current, id])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !name.trim() || selectedProjects.length === 0) return
    setBusy(true); setCreateError(null); setNewToken(null)
    try {
      const created = await maintenanceApi.createIntakeSource(
        { name: name.trim(), project_ids: selectedProjects, auto_dispatch: autoDispatch },
        { csrfToken, onUnauthorized },
      )
      if (created.token) setNewToken({ id: created.id, token: created.token })
      setName(''); setSelectedProjects([]); setAutoDispatch(false)
      load()
    } catch (cause) {
      setCreateError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id: string) => {
    try { await maintenanceApi.revokeIntakeSource(id, { csrfToken, onUnauthorized }); load() }
    catch (cause) { setError(errorText(cause)) }
  }

  const copyToken = async () => {
    if (!newToken) return
    try { await navigator.clipboard.writeText(newToken.token); setCopied(true); window.setTimeout(() => setCopied(false), 2000) }
    catch { /* clipboard 不可用时静默忽略 */ }
  }

  return (
    <section className="wb-card" aria-labelledby="intake-sources-title" data-testid="intake-sources-admin">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">管理员</span><h2 id="intake-sources-title">接入来源</h2></div>
      </div>

      {error && <ErrorNotice message={error} />}

      {sources && sources.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          {sources.map(s => (
            <div className="ms-source-row" key={s.id}>
              <div>
                <strong>{s.name}</strong>
                <small>令牌 {s.token_hint}　{s.auto_dispatch ? '自动派发' : '人工派发'}{s.revoked_at ? '　已撤销' : ''}</small>
              </div>
              {!s.revoked_at && <button className="wb-button wb-button-secondary" onClick={() => void revoke(s.id)}>撤销</button>}
            </div>
          ))}
        </div>
      )}
      {sources && sources.length === 0 && <p className="wb-runtime-note">还没有接入来源。</p>}

      {newToken && (
        <div className="wb-notice" role="status" data-testid="new-token-box">
          <strong>令牌只在此刻显示一次，请立即保存。</strong>
          <div className="ms-token-box" style={{ marginTop: 8 }}>
            <span style={{ flex: 1 }}>{newToken.token}</span>
            <button type="button" className="wb-button wb-button-secondary" onClick={() => void copyToken()}>{copied ? '已复制' : '复制'}</button>
          </div>
        </div>
      )}

      <form className="wb-form" onSubmit={submit} data-testid="intake-source-form">
        <label>
          来源名称
          <input required value={name} onChange={e => setName(e.target.value)} placeholder="例如 告警平台" />
        </label>
        <div>
          <span className="wb-eyebrow">可提交的项目</span>
          <div className="wb-check-options">
            {projects.map(p => (
              <label className="wb-checkbox" key={p.project_id}>
                <input type="checkbox" checked={selectedProjects.includes(p.project_id)} onChange={() => toggleProject(p.project_id)} />
                {p.name}
              </label>
            ))}
          </div>
        </div>
        <label className="wb-checkbox">
          <input type="checkbox" checked={autoDispatch} onChange={e => setAutoDispatch(e.target.checked)} />
          自动派发执行（默认关闭）
        </label>
        {createError && <ErrorNotice message={createError} />}
        <div className="wb-form-actions">
          <button className="wb-button wb-button-primary" disabled={busy || !name.trim() || selectedProjects.length === 0}>{busy ? '创建中…' : '创建接入来源'}</button>
        </div>
      </form>
    </section>
  )
}

// --- 接收记录 ---

function RequirementsTable({ csrfToken, onUnauthorized, projects, refreshKey, onDispatched }: PageProps & {
  projects: RepoView[]
  refreshKey: number
  onDispatched: () => void
}) {
  const [requirements, setRequirements] = useState<Requirement[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const projectNames = new Map(projects.map(p => [p.project_id, p.name]))

  useEffect(() => {
    const controller = new AbortController()
    maintenanceApi.requirements(undefined, { onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setRequirements(result.requirements) })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
    return () => controller.abort()
  }, [onUnauthorized, refreshKey])

  const dispatch = async (id: string) => {
    setBusyId(id)
    try { await maintenanceApi.dispatchRequirement(id, { csrfToken, onUnauthorized }); onDispatched() }
    catch (cause) { setError(errorText(cause)) }
    finally { setBusyId(null) }
  }

  return (
    <section className="wb-card wb-run-table-card" aria-label="接收记录">
      <div className="wb-card-head"><div><span className="wb-eyebrow">接收记录</span><h2>需求接收记录</h2></div></div>
      {error && <ErrorNotice message={error} />}
      {requirements === null && !error && <div className="wb-list-placeholder"><span /><span /><span /></div>}
      {requirements !== null && requirements.length === 0 && (
        <EmptyState title="还没有接收记录" description="通过主动提交或自动接入的需求会出现在这里。" />
      )}
      {requirements !== null && requirements.length > 0 && (
        <div className="wb-table-wrap">
          <table className="wb-table">
            <thead>
              <tr><th>来源</th><th>项目</th><th>内容摘要</th><th>接收时间</th><th>状态</th><th>任务</th></tr>
            </thead>
            <tbody>
              {requirements.map(r => (
                <tr key={r.requirement_id}>
                  <td>{r.source.name}（{r.source.kind}）</td>
                  <td>{r.project_name ?? projectNames.get(r.project_id) ?? r.project_id}</td>
                  <td>{r.content.slice(0, 40)}{r.content.length > 40 ? '…' : ''}</td>
                  <td>{formatDate(r.received_at)}</td>
                  <td>
                    {r.status_label || RECEIPT_STATUS_LABEL[r.status]}
                    {r.status === 'pending_dispatch' && (
                      <button className="wb-button wb-button-secondary" style={{ marginLeft: 8 }} disabled={busyId === r.requirement_id} onClick={() => void dispatch(r.requirement_id)}>
                        {busyId === r.requirement_id ? '派发中…' : '派发执行'}
                      </button>
                    )}
                  </td>
                  <td>{r.task_id ? <Link to={`/maintenance/${encodeURIComponent(r.task_id)}`}>查看任务</Link> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export default function IntakePage(props: PageProps) {
  const { onUnauthorized, user } = props
  const [projects, setProjects] = useState<RepoView[]>([])
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const isAdmin = user?.role !== 'member'

  useEffect(() => {
    const controller = new AbortController()
    maintenanceApi.repos({ onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setProjects(result.repos) })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setProjectsError(errorText(cause))
      })
    return () => controller.abort()
  }, [onUnauthorized])

  return (
    <div className="wb-page">
      <PageHeader title="需求接入" description="人工在这里提交诉求；机器来源通过统一接入契约提交。已接收不等于已执行。" />

      {projectsError && <ErrorNotice message={projectsError} />}
      {projects.length === 0 && !projectsError && (
        <div className="wb-card"><EmptyState title="还没有可提交的项目" description="请先在「维护代码库」登记并完成分析。" /></div>
      )}

      {projects.length > 0 && (
        <div className="ms-two-col">
          <ManualSubmit {...props} projects={projects} onSubmitted={() => setRefreshKey(k => k + 1)} />
          <AutomaticIntakeInfo />
        </div>
      )}

      {isAdmin && <div style={{ marginTop: 18 }}><IntakeSourcesAdmin {...props} projects={projects} /></div>}

      <div style={{ marginTop: 18 }}>
        <RequirementsTable {...props} projects={projects} refreshKey={refreshKey} onDispatched={() => setRefreshKey(k => k + 1)} />
      </div>
    </div>
  )
}
