import Icon, { CategoryBadge, packMark } from './Icon'
import { useWorkTitle } from '../conversation/title-context'
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import { ErrorNotice, EmptyState, formatDate, PageHeader, errorText, type PageProps } from './ui'
import AgentManifest from './AgentManifest'
import AgentEvolution from './AgentEvolution'
import NativePackImport from './NativePackImport'
import AgentMetadataEditor from './AgentMetadataEditor'
import AgentAssets from './AgentAssets'
import SkillIngestion from './SkillIngestion'
import SkillUploadFeedback from './SkillUploadFeedback'
import { ENV_LABEL, packsBase, type PackBinding } from './pack-types'
import { capabilityHref } from './capability-links'
import './agents.css'

type Agent = { builtin_pack?: string; id: string; name: string; purpose?: string; active_version?: number; updated_at?: string; version?: AgentVersion; recent_tasks?: Array<{ id?: string; title?: string; status?: string }> }
type AgentVersion = { id?: string; agent_id?: string; version: number; instructions?: string; model_settings?: Record<string, unknown>; tool_scope?: string[]; acceptance?: string[]; delivery?: Record<string, unknown>; skill_ids?: string[]; source?: string; created_at?: string }
type Message = { feedback_status?: 'pending' | 'adopted'; feedback_run_id?: string; id?: string; role: string; content: string; at?: string; created_at?: string; run_id?: string; status?: string; job_id?: string; attachment?: { name?: string; id?: string }; progress?: { label?: string; status?: string }; deliverables?: Array<{ name?: string; url?: string; href?: string; kind?: string }> }
type Conversation = { feedback_error?: string; pending_feedback_count?: number; id: string; agent_id: string; mode: string; project_id?: string; messages: Message[]; run_id?: string | null; updated_at?: string }
type Module = { id: string; version: number; name: string; category: string; description: string }
type Capability = { id: string; name: string; source_run_id?: string }

const base = '/api/v4'
const pathAgent = (id: string) => `${base}/agents/${encodeURIComponent(id)}`

function asList<T>(value: unknown, key: string): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object' && Array.isArray((value as Record<string, unknown>)[key])) return (value as Record<string, unknown>)[key] as T[]
  return []
}

export function unwrapAgentMessageResponse(value: { conversation?: Conversation; run?: Run | null; draft?: unknown; needs_project?: boolean }): { conversation: Conversation | null; run: Run | null; draft?: unknown; needsProject: boolean } {
  return { conversation: value.conversation || null, run: value.run || null, draft: value.draft, needsProject: value.needs_project === true }
}

export function feedbackStatusLabel(status?: string): string | null {
  if (status === 'pending') return '补充已保存，当前运行成功结束后自动接续；如需人工处理会保留等待。'
  if (status === 'adopted') return '补充已纳入后续任务。'
  return null
}

export function artifactLinks(artifacts: Record<string, unknown> | null | undefined): Array<{ label: string; href: string }> {
  if (!artifacts) return []
  return Object.entries(artifacts).filter(([, value]) => typeof value === 'string' && /^https?:\/\//.test(value)).map(([key, value]) => ({ label: key, href: value as string }))
}

export function isTerminalRun(status?: string): boolean { return ['published', 'failed', 'cancelled', 'discarded', 'ready_for_review', 'interrupted'].includes(status || '') }
export function draftCanApply(draft: { revision: number; conflicts?: unknown[] }): boolean { return draft.revision > 0 && (draft.conflicts || []).length === 0 }

// ---- Add Ability: three progressive-disclosure sources ----

type AbilitySource = 'upload' | 'team' | 'dev'

/** Pack detail link that remembers which role sent you, so the detail page can
 *  bring you back and pre-select the target instead of asking again. */
function packHref(packId: string, agentId: string, tab?: string) {
  const params = new URLSearchParams({ agent_id: agentId, ...(tab ? { tab } : {}) })
  return `/ability-center/packs/${encodeURIComponent(packId)}?${params}`
}

function AddAbilityPanel({ agentId, onChanged, ...props }: PageProps & { agentId: string; onChanged: () => void }) {
  const [source, setSource] = useState<AbilitySource | null>(null)
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [uploadMessage, setUploadMessage] = useState('')
  const [uploadRunId, setUploadRunId] = useState('')
  const [epoch, setEpoch] = useState(0)
  const [preflight, setPreflight] = useState<{ ready: boolean; message: string } | null>(null)

  // Team capabilities
  const [modules, setModules] = useState<Module[]>([])
  const [capabilities, setCaps] = useState<Capability[]>([])
  const [catalog, setCatalog] = useState<PackSummary[]>([])
  const [boundIds, setBoundIds] = useState<string[]>([])
  const [teamError, setTeamError] = useState('')
  const [teamLoaded, setTeamLoaded] = useState(false)
  const [binding, setBinding] = useState('')
  const [bindError, setBindError] = useState('')

  // Dev results: real candidates this user maintains, not what is already attached.
  const [candidates, setCandidates] = useState<PackSummary[]>([])
  const [devError, setDevError] = useState('')
  const [devLoaded, setDevLoaded] = useState(false)

  useEffect(() => {
    const c = new AbortController()
    void request<{ ready: boolean; message: string }>(`/api/v4/agents/${encodeURIComponent(agentId)}/abilities/preflight`, { signal: c.signal, onUnauthorized: props.onUnauthorized })
      .then(r => { if (!c.signal.aborted) setPreflight(r) })
      .catch(e => { if (!c.signal.aborted) setUploadError(errorText(e)) })
    return () => c.abort()
  }, [agentId, props.onUnauthorized])

  // Load team data when that source is selected
  useEffect(() => {
    if (source !== 'team') return
    const c = new AbortController()
    setTeamLoaded(false); setTeamError('')
    Promise.all([
      request<{ modules: Module[] }>(`/api/v4/modules?agent_id=${encodeURIComponent(agentId)}`, { signal: c.signal, onUnauthorized: props.onUnauthorized }),
      request<{ capabilities: Capability[] }>('/api/v3/capabilities', { signal: c.signal, onUnauthorized: props.onUnauthorized }),
      request<{ packs: PackSummary[] }>(packsBase, { signal: c.signal, onUnauthorized: props.onUnauthorized }),
      request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`, { signal: c.signal, onUnauthorized: props.onUnauthorized }),
    ]).then(([mods, caps, all, bound]) => {
      if (c.signal.aborted) return
      setModules(mods.modules || []); setCaps(caps.capabilities || [])
      setCatalog(all.packs || []); setBoundIds((bound.bindings || []).map(b => b.pack_id))
      setTeamLoaded(true)
    }).catch(e => { if (!c.signal.aborted) setTeamError(errorText(e)) })
    return () => c.abort()
  }, [source, agentId, props.onUnauthorized, epoch])

  // Load dev packs when that source is selected
  useEffect(() => {
    if (source !== 'dev') return
    const c = new AbortController()
    setDevLoaded(false); setDevError('')
    request<{ packs: PackSummary[] }>(packsBase, { signal: c.signal, onUnauthorized: props.onUnauthorized })
      .then(r => {
        if (c.signal.aborted) return
        // Development results still on their way to a published version: these are
        // the ones you continue (validate -> publish -> attach), not the finished
        // tools already attached to this role.
        setCandidates((r.packs || []).filter(pk => !pk.published_version))
        setDevLoaded(true)
      })
      .catch(e => { if (!c.signal.aborted) setDevError(errorText(e)) })
    return () => c.abort()
  }, [source, agentId, props.onUnauthorized, epoch])

  /** Attach a published pack to THIS agent, using the existing detail and
   *  bindings endpoints. The role is fixed by the page, so nothing has to be
   *  re-picked, and the attached list refreshes in place. */
  const attachPack = async (packId: string) => {
    if (binding) return
    setBinding(packId); setBindError('')
    try {
      const detail = await request<PackDetail>(`${packsBase}/${encodeURIComponent(packId)}`, { onUnauthorized: props.onUnauthorized })
      const latest = (detail.versions || [])[0]
      if (!latest) throw new Error('该职能包还没有已发布版本')
      const current = await request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`, { onUnauthorized: props.onUnauthorized })
      const existing = (current.bindings || []).find(b => b.pack_id === packId)
      await request(`${packsBase}/bindings`, {
        method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized,
        body: { agent_id: agentId, version_id: latest.id, expected_revision: existing?.revision ?? 0 },
      })
      setEpoch(v => v + 1); onChanged()
    } catch (cause) { setBindError(errorText(cause)) } finally { setBinding('') }
  }

  const submitUpload = async (file: File) => {
    if (uploadBusy) return
    setUploadBusy(true); setUploadError(''); setUploadMessage(''); setUploadRunId('')
    try {
      const body = new FormData(); body.append('file', file)
      const result = await request<{ channel: string; message: string; ingestion?: { run_id: string } }>(`/api/v4/agents/${encodeURIComponent(agentId)}/abilities`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body })
      setUploadMessage(result.message); setUploadRunId(result.ingestion?.run_id || '')
      setEpoch(v => v + 1); onChanged()
    } catch (e) { setUploadError(errorText(e)) } finally { setUploadBusy(false) }
  }

  return (
    <section className="agent-section" aria-label="添加能力">
      <h2>添加能力</h2>
      <p className="agent-section-desc">选择来源后展开对应表单。一次只显示一个来源。</p>
      <div className="agent-source-tabs" role="group" aria-label="能力来源">
        {([['upload', '上传包'], ['team', '团队已有能力'], ['dev', '开发成果']] as const).map(([key, label]) => (
          <button key={key} type="button"
            className={`cv-btn ${source === key ? 'cv-btn-primary' : 'cv-btn-secondary'}`}
            aria-pressed={source === key}
            onClick={() => setSource(source === key ? null : key)}>
            {label}
          </button>
        ))}
      </div>

      {/* ---- Upload source ---- */}
      {source === 'upload' && (
        <div className="agent-source-form" data-testid="source-upload">
          <p>上传 Skill ZIP 文件。平台导出的职能体包（会新建角色）请使用列表页的"导入职能体"入口。</p>
          {preflight?.ready === false && <p role="alert">{preflight.message}</p>}
          <label>Skill ZIP 文件
            <input type="file" accept=".zip" disabled={uploadBusy || preflight?.ready === false}
              onChange={e => { const file = e.target.files?.[0]; if (file) void submitUpload(file); e.target.value = '' }} />
          </label>
          {uploadBusy && <p role="status">正在识别并保存素材…</p>}
          {uploadError && <SkillUploadFeedback message={uploadError} />}
          {uploadMessage && <p role="status">{uploadMessage} {uploadRunId ? <Link to={`/runs/${uploadRunId}`}>查看适配运行</Link> : null}</p>}
        </div>
      )}

      {/* ---- Team existing capabilities ---- */}
      {source === 'team' && (
        <div className="agent-source-form" data-testid="source-team">
          <p>从团队已有的方法模块（Skill）和沉淀能力中选择。模块直接在岗位清单中引用；沉淀能力需经升格流程成为 Skill 后加入。职能包（独立工具）通过挂靠机制绑定。</p>
          {teamError && <ErrorNotice message={teamError} />}
          {!teamLoaded && !teamError && <p>正在加载…</p>}
          {teamLoaded && (
            <>
              <h3>方法模块（Skill） · {modules.length} 个</h3>
              {modules.length > 0 ? (
                <ul className="agent-team-list">
                  {modules.map(m => (
                    <li key={m.id}>
                      <Link to={`${capabilityHref('modules', m.id)}&agent_id=${encodeURIComponent(agentId)}`}>{m.name}</Link>
                      <small> v{m.version} · {m.description?.slice(0, 60) || m.category}</small>
                    </li>
                  ))}
                </ul>
              ) : <p>团队还没有方法模块。</p>}
              <p style={{ marginTop: 12 }}>选好后在上方「工作规范」的岗位清单里加载它。</p>

              <h3 style={{ marginTop: 16 }}>沉淀能力 · {capabilities.length} 个</h3>
              {capabilities.length > 0 ? (
                <ul className="agent-team-list">
                  {capabilities.map(c => (
                    <li key={c.id}>
                      <Link to={`${capabilityHref('capabilities', c.id)}&agent_id=${encodeURIComponent(agentId)}`}>{c.name}</Link>
                      {c.source_run_id && <small> · <Link to={`/runs/${encodeURIComponent(c.source_run_id)}`}>来源运行</Link></small>}
                    </li>
                  ))}
                </ul>
              ) : <p>还没有运行沉淀的能力。</p>}
              <p className="agent-section-note">沉淀能力本身面向项目使用。要将其加入职能体，需在能力详情里"升格为 Skill"并通过进化提案流程。</p>

              <h3 style={{ marginTop: 16 }}>可用工具</h3>
              {bindError && <ErrorNotice message={bindError} />}
              {(() => {
                const available = catalog.filter(pk => pk.published_version && !boundIds.includes(pk.id))
                const pending = catalog.filter(pk => !pk.published_version)
                return <>
                  {available.length > 0 ? (
                    <ul className="agent-team-list">
                      {available.map(pk => (
                        <li key={pk.id}>
                          <Link to={packHref(pk.id, agentId)}>{pk.name}</Link>
                          <small> v{pk.published_version} · {pk.purpose?.slice(0, 60) || '独立工具'}</small>
                          <button type="button" className="cv-btn cv-btn-secondary agent-attach-btn"
                            disabled={Boolean(binding)} onClick={() => void attachPack(pk.id)}>
                            {binding === pk.id ? '挂靠中…' : '挂靠到本职能体'}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : <p>没有可直接挂靠的工具。已挂靠的在上方「已挂靠工具」区管理。</p>}
                  {pending.length > 0 && <>
                    <h3 style={{ marginTop: 16 }}>还在准备中的工具</h3>
                    <p className="agent-section-note">这些还没有可用版本。打开后按原有流程验证并发布，完成后回到这里挂靠。</p>
                    <ul className="agent-team-list">
                      {pending.map(pk => (
                        <li key={pk.id}>
                          <Link to={packHref(pk.id, agentId)}>{pk.name}</Link>
                          <small> · 待验证发布</small>
                        </li>
                      ))}
                    </ul>
                  </>}
                </>
              })()}
            </>
          )}
        </div>
      )}

      {/* ---- Dev results ---- */}
      {source === 'dev' && (
        <div className="agent-source-form" data-testid="source-dev">
          <p>复用真实开发成果沉淀的工具包：从运行成果中选择 → 候选 → 验证 → 发布 → 挂靠到当前职能体。</p>
          {devError && <ErrorNotice message={devError} />}
          {!devLoaded && !devError && <p>正在加载…</p>}
          {devLoaded && candidates.length === 0 && (
            <div className="agent-dev-empty">
              <p>还没有待继续的开发成果。</p>
              <p>先在项目里完成一次开发任务，在成果页把它沉淀为工具包；回到这里就能继续验证、发布并挂靠。
                <Link to="/runs" style={{ marginLeft: 6 }}>打开运行记录</Link></p>
            </div>
          )}
          {devLoaded && candidates.length > 0 && (
            <ul className="agent-team-list">
              {candidates.map(pk => (
                <li key={pk.id}>
                  <Link to={packHref(pk.id, agentId)}>{pk.name}</Link>
                  <small> · {pk.purpose?.slice(0, 60) || '待验证发布'}</small>
                  <small> · 打开后验证并发布，完成即可挂靠回本职能体</small>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Skill ingestion drafts always visible below */}
      <AgentAssets agentId={agentId} refresh={epoch} {...props} />
      <SkillIngestion key={`${agentId}:${epoch}`} agentId={agentId} onSigned={onChanged} {...props} />
    </section>
  )
}

// ---- Attached tools section ----

function AttachedTools({ agentId, ...props }: PageProps & { agentId: string }) {
  const [bindings, setBindings] = useState<PackBinding[] | null>(null)
  const [error, setError] = useState('')
  const load = useCallback((signal?: AbortSignal) => {
    return request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`, { signal, onUnauthorized: props.onUnauthorized })
      .then(r => setBindings(r.bindings || []))
      .catch(e => { if (!signal?.aborted) setError(errorText(e)) })
  }, [agentId, props.onUnauthorized])
  useEffect(() => { const c = new AbortController(); void load(c.signal); return () => c.abort() }, [load])

  return (
    <section className="agent-section" aria-label="已挂靠工具">
      <h2>已挂靠工具</h2>
      <p className="agent-section-desc">通过职能包挂靠机制安装的独立工具。版本在挂靠时冻结；升级和解除挂靠沿用现有确认流程。</p>
      {error && <ErrorNotice message={error} />}
      {bindings === null && !error && <p>正在加载…</p>}
      {bindings && bindings.length === 0 && <p>尚无已挂靠工具。可在"添加能力"中通过开发成果或团队已有能力挂靠。</p>}
      {bindings && bindings.length > 0 && (
        <div className="agent-tools-list">
          {bindings.map(b => (
            <div key={b.id} className="agent-tool-row">
              <div className="agent-tool-info">
                <strong>{b.pack_name}</strong>
                <span className="agent-tool-version">v{b.version}</span>
                {b.upgrade_available && <Link className="agent-tool-upgrade" to={`/ability-center/packs/${encodeURIComponent(b.pack_id)}?tab=versions`}>可升级到 v{b.latest_version}</Link>}
              </div>
              <div className="agent-tool-status">
                {/* Only an explicit `ready` may read as usable. unchecked (which the
                    backend also returns when a check went stale or the runtime
                    changed), checking and a missing field are NOT readiness. */}
                {b.environment?.status === 'ready'
                  ? <span className="agent-tool-ok">{ENV_LABEL.ready}</span>
                  : b.environment?.status === 'unavailable'
                    ? <span className="agent-tool-warn">{ENV_LABEL.unavailable}：{b.environment.missing?.join('、') || '未知依赖'}</span>
                    : <span className="agent-tool-pending">{ENV_LABEL[b.environment?.status ?? 'unchecked']}</span>}
                {b.environment?.stale && <span className="agent-tool-stale">{b.environment.stale_reason || '上次检查结果已过期'}</span>}
              </div>
              <Link className="cv-btn cv-btn-secondary" style={{ fontSize: 13, padding: '6px 10px' }}
                to={`/ability-center/packs/${encodeURIComponent(b.pack_id)}`}>管理</Link>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ---- Management page (detail view) ----

function AgentManagementPage({ agent: initialAgent, props }: { agent: Agent; props: PageProps }) {
  const [agent, setAgent] = useState(initialAgent)
  // This component holds the live agent, so the shell breadcrumb
  // (职能体 > 当前名称 > 管理) is fed from here: a rename shows up at once
  // instead of only after a reload.
  const setWorkTitle = useWorkTitle()
  useEffect(() => { setWorkTitle(agent.name); return () => setWorkTitle(null) }, [agent.name, setWorkTitle])
  const [editingMeta, setEditingMeta] = useState(false)
  const [manifestEpoch, setManifestEpoch] = useState(0)
  const isAdmin = props.user?.role !== 'member'

  const refreshAgent = useCallback(async () => {
    try {
      const fresh = await request<Agent>(pathAgent(agent.id), { onUnauthorized: props.onUnauthorized })
      setAgent(fresh)
    } catch { /* keep current */ }
  }, [agent.id, props.onUnauthorized])

  const mark = packMark(agent)

  return (
    <div className="agent-mgmt">
      {/* ---- Section 1: Basic Info ---- */}
      <section className="agent-section" aria-label="基本信息">
        <h2>基本信息</h2>
        {editingMeta ? (
          <AgentMetadataEditor
            agent={agent}
            csrfToken={props.csrfToken}
            onUnauthorized={props.onUnauthorized}
            onSaved={(updated) => { setAgent(prev => ({ ...prev, ...updated })); setEditingMeta(false) }}
            onCancel={() => setEditingMeta(false)}
          />
        ) : (
          <div className="agent-info-card">
            <div className="agent-info-head">
              <CategoryBadge avatar={Array.from(agent.name)[0]} mark={mark} />
              <div>
                <h3>{agent.name}</h3>
                <p className="agent-info-purpose">{agent.purpose || '还没有用途说明。'}</p>
              </div>
            </div>
            <div className="agent-info-meta">
              <span>当前版本 <strong>v{agent.version?.version ?? agent.active_version ?? '—'}</strong></span>
              {agent.updated_at && <span>更新于 {formatDate(agent.updated_at)}</span>}
            </div>
            {isAdmin && (
              <button className="cv-btn cv-btn-secondary" style={{ marginTop: 8 }} onClick={() => setEditingMeta(true)}>
                编辑名称与用途
              </button>
            )}
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <Link className="cv-btn cv-btn-primary" to={`/agents/${encodeURIComponent(agent.id)}/chat`}>
            开始对话 <Icon name="arrow" width={15} height={15} />
          </Link>
          {agent.builtin_pack && (
            <a className="cv-agent-dl" style={{ marginLeft: 12 }}
              href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}>
              <Icon name="download" width={14} height={14} /> 导出职能包
            </a>
          )}
        </div>
      </section>

      {/* ---- Section 2: Work Specs ---- */}
      <section className="agent-section" aria-label="工作规范">
        <h2>工作规范</h2>
        <p className="agent-section-desc">已启用的 Skill/方法，现有适配/验收/版本与维护入口。</p>
        <AgentManifest key={`${agent.id}:${manifestEpoch}`} agentId={agent.id} {...props} />
        <AgentEvolution key={agent.id} agentId={agent.id} {...props} onChanged={() => { setManifestEpoch(v => v + 1); void refreshAgent() }} />
      </section>

      {/* ---- Section 3: Attached Tools ---- */}
      <AttachedTools agentId={agent.id} {...props} />

      {/* ---- Add Ability ---- */}
      {isAdmin && <AddAbilityPanel agentId={agent.id} {...props} onChanged={() => { setManifestEpoch(v => v + 1); void refreshAgent() }} />}
    </div>
  )
}

// ---- New Agent form (kept for list view) ----

function NewAgent({ csrfToken, onUnauthorized, onCreated, onCancel }: PageProps & { onCreated: (agent: Agent) => void; onCancel: () => void }) {
  const [name, setName] = useState(''); const [purpose, setPurpose] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null)
  const submit = async (event: FormEvent) => { event.preventDefault(); if (!name.trim()) return; setBusy(true); setError(null); try { const result = await request<Agent>(`${base}/agents`, { method: 'POST', csrfToken, onUnauthorized, body: { name: name.trim(), purpose: purpose.trim(), identity: purpose.trim() } }); onCreated(result) } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(false) } }
  return <div className="agent-create-card wb-card"><div className="agent-card-kicker">新建职能体</div><h2>先告诉它擅长什么</h2><p>创建岗位而非一次任务：写下长期职责，具体做法通过 skill 清单组合。</p><form onSubmit={submit}><label>名称<input required maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：工业格式维护员" /></label><label>用途<textarea maxLength={500} rows={3} value={purpose} onChange={(event) => setPurpose(event.target.value)} placeholder="它适合解决哪些问题？" /></label>{error && <ErrorNotice message={error} />}<div className="agent-form-actions"><button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button><button className="wb-button wb-button-primary" disabled={busy}>{busy ? '创建中…' : '创建并开始'}</button></div></form></div>
}

// ---- Entry point ----

export default function AgentsPage(props: PageProps) {
  const [locationQuery] = useSearchParams()
  const params = useParams()
  // The shell's breadcrumb shows 职能体 > 当前名称 > 管理; only this page knows the name.
  const setWorkTitle = useWorkTitle()
  const navigate = useNavigate()
  const [agents, setAgents] = useState<Agent[]>([])
  const [selected, setSelected] = useState<Agent | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // The management page owns the live name; here we only make sure the catalog
  // view never keeps a stale one.
  useEffect(() => { if (!params.agentId) setWorkTitle(null) }, [params.agentId, setWorkTitle])

  const load = useCallback(() => {
    setLoading(true)
    void request<unknown>(`${base}/agents`, { onUnauthorized: props.onUnauthorized })
      .then((value) => {
        const list = asList<Agent>(value, 'agents')
        setAgents(list)
        const wanted = params.agentId ? list.find((item) => item.id === params.agentId) : undefined
        setSelected(wanted || null)
      })
      .catch((cause) => {
        if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause))
      })
      .finally(() => setLoading(false))
  }, [params.agentId, props.onUnauthorized])

  useEffect(() => { load() }, [load])

  const choose = (id: string) => navigate(`/agents/${encodeURIComponent(id)}`)

  // Detail view: management page for a specific agent (no left sidebar)
  if (params.agentId) {
    return (
      <div className="wb-page agent-page">
        {loading && !selected && <div className="wb-card agent-loading">正在读取职能体…</div>}
        {!loading && !selected && !error && <ErrorNotice message="未找到这个职能体，请返回列表选择。" />}
        {error && <ErrorNotice message={error} />}
        {/* Keyed by id: navigating a1 -> a2 inside one route tree must rebuild the
            management state, otherwise edits and bindings keep writing to the
            agent that was mounted first. */}
        {selected && <AgentManagementPage key={selected.id} agent={selected} props={props} />}
      </div>
    )
  }

  // List view
  return (
    <div className="wb-page agent-page">
      <PageHeader
        title="职能体工作台"
        description="让反复出现的业务，有一个持续维护的入口。"
        actions={<>
          <button className="wb-button wb-button-primary" onClick={() => setCreating(true)}><Icon name="plus" /> 新建职能体</button>
          {props.user?.role === 'admin' && <button className="wb-button wb-button-secondary" onClick={() => setImporting(v => !v)}>导入职能体</button>}
        </>}
      />
      {error && <ErrorNotice message={error} />}
      {locationQuery.get('import') === 'external' && <p>请先选择或新建职能体，再用"添加能力"上传外部包。</p>}
      {locationQuery.get('project') && <SkillIngestion {...props} reviewOnly />}
      {importing && props.user?.role === 'admin' && <section className="wb-card" aria-label="导入整包职能体"><NativePackImport {...props} onImported={choose} /></section>}
      {creating && <NewAgent {...props} onCancel={() => setCreating(false)} onCreated={(agent) => { setCreating(false); setAgents((current) => [agent, ...current]); navigate(`/agents/${encodeURIComponent(agent.id)}`) }} />}
      {loading && agents.length === 0 && <div className="wb-card agent-loading">正在读取职能体…</div>}
      {!loading && !error && agents.length === 0 && !creating && <div className="wb-card"><EmptyState title="还没有职能体" description="从一个名字和用途开始，之后可以在维护对话里教会它。" action={<button className="wb-button wb-button-primary" onClick={() => setCreating(true)}>创建第一个职能体</button>} /></div>}
      {agents.length > 0 && (
        <section className="agent-catalog" aria-label="职能体目录">
          {agents.map(agent => (
            <article className="agent-catalog-card" key={agent.id}>
              <header>
                <CategoryBadge avatar={Array.from(agent.name)[0]} mark={packMark(agent)} />
                <h2>{agent.name}</h2>
                <small>v{agent.active_version ?? agent.version?.version ?? '—'}</small>
              </header>
              <div className="agent-catalog-purpose">
                <span>业务用途</span>
                <p>{agent.purpose || '还没有用途说明，可以在管理页补充。'}</p>
              </div>
              <footer>
                <Link className="wb-button wb-button-primary" to={`/agents/${encodeURIComponent(agent.id)}/chat`}>开始对话</Link>
                <Link className="wb-button wb-button-secondary" to={`/agents/${encodeURIComponent(agent.id)}`}>管理职能体</Link>
              </footer>
              {agent.builtin_pack && <a className="agent-pack-download wb-text-link" title="下载平台原始模板" href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}>下载内置职能包 ZIP <Icon name="download" /></a>}
            </article>
          ))}
        </section>
      )}
      <section className="wb-purpose-band wb-purpose-plain">
        <span className="wb-purpose-symbol" aria-hidden="true"><Icon name="plus" /></span>
        <div><h2>普通开发任务，直接开始就好</h2><p>不需要先创建职能体；在项目中描述你的目标即可。</p></div>
        <Link className="wb-button wb-button-secondary" to="/projects">前往项目</Link>
      </section>
    </div>
  )
}
