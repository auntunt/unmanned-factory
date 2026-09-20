import Icon, { CategoryBadge, packMark } from './Icon'
import { useWorkTitle } from '../conversation/title-context'
import { useCallback, useEffect, useRef, useState } from 'react'
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
import { ENV_LABEL, packsBase, type PackBinding, type PackSummary, type PackDetail } from './pack-types'
import { capabilityHref } from './capability-links'
import { ProjectForm } from './ProjectsPage'
import './agents.css'

type Agent = { builtin_pack?: string; id: string; name: string; purpose?: string; active_version?: number; updated_at?: string; version?: AgentVersion; recent_tasks?: Array<{ id?: string; title?: string; status?: string }> }
type AgentVersion = { id?: string; agent_id?: string; version: number; instructions?: string; model_settings?: Record<string, unknown>; tool_scope?: string[]; acceptance?: string[]; delivery?: Record<string, unknown>; skill_ids?: string[]; source?: string; created_at?: string }
type Message = { feedback_status?: 'pending' | 'adopted'; feedback_run_id?: string; id?: string; role: string; content: string; at?: string; created_at?: string; run_id?: string; status?: string; job_id?: string; attachment?: { name?: string; id?: string }; progress?: { label?: string; status?: string }; deliverables?: Array<{ name?: string; url?: string; href?: string; kind?: string }> }
type Conversation = { feedback_error?: string; pending_feedback_count?: number; id: string; agent_id: string; mode: string; project_id?: string; messages: Message[]; run_id?: string | null; updated_at?: string }
type Module = { id: string; version: number; name: string; category: string; description: string }
type Capability = { id: string; name: string; source_run_id?: string }
type Draft = { id?: string; agent_id: string; base_version?: number; revision: number; patch: Record<string, unknown>; conflicts?: Array<{ title?: string; detail?: string; choice?: string } | string>; explanation?: Array<{ text?: string; source?: string } | string> }
type MaintenanceJob = { id: string; status: 'pending' | 'running' | 'cancel_requested' | 'completed' | 'failed' | 'cancelled' | 'interrupted'; error?: string; result?: { draft?: Draft } }

const base = '/api/v4'
const pathAgent = (id: string) => `${base}/agents/${encodeURIComponent(id)}`
const pathConversation = (id: string) => `${base}/conversations/${encodeURIComponent(id)}`

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

// ---- Restored maintenance components (DraftCard, ModelSettings) ----

function displayChange(key: string, value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map((item) => typeof item === 'string' ? item : String(item)).join('、')
  if (key === 'model_settings' && value && typeof value === 'object') { const settings = value as Record<string, unknown>; const defaults = settings.default as Record<string, unknown> | undefined; return defaults?.model ? `默认：${String(defaults.model)}` : '沿用平台默认模型' }
  if (key === 'delivery' && value && typeof value === 'object') return Object.keys(value as Record<string, unknown>).join('、') || '已更新交付约定'
  return '已更新'
}

function DraftCard({ draft, version, props, agentId, onChanged }: { draft: Draft; version?: AgentVersion; props: PageProps; agentId: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null)
  const apply = async () => { setBusy(true); setError(null); try { await request(`${pathAgent(agentId)}/draft/apply`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { expected_revision: draft.revision, idempotency_key: `apply-${draft.id || agentId}-${draft.revision}` } }); onChanged() } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(false) } }
  const conflicts = draft.conflicts ?? []; const explanation = draft.explanation ?? []; const patchEntries = Object.entries(draft.patch ?? {}).filter(([key, value]) => ['instructions', 'acceptance', 'delivery', 'tool_scope', 'model_settings', 'purpose'].includes(key) && JSON.stringify(value) !== JSON.stringify(version?.[key as keyof AgentVersion]))
  return <section className="agent-change-card" aria-label="待应用的维护变更"><div className="agent-change-head"><div><span className="agent-card-kicker">维护变更 · 草稿修订 {draft.revision}</span><h3>{patchEntries.length ? '这次对话整理出的具体变化' : '还没有待应用的变化'}</h3></div><span className="agent-draft-state">{conflicts.length ? '需先处理冲突' : draft.revision > 0 ? '待应用' : '已同步'}</span></div>{patchEntries.length > 0 && <div className="agent-change-list">{patchEntries.map(([key, value]) => <div className="agent-change-row" key={key}><strong>{key === 'instructions' ? '工作步骤' : key === 'acceptance' ? '验收条件' : key === 'delivery' ? '交付约定' : key === 'model_settings' ? '模型设置' : key === 'tool_scope' ? '可用工具' : key}</strong><span>{displayChange(key, value)}</span></div>)}</div>}{explanation.length > 0 && <div className="agent-change-sources"><strong>来源与范围</strong>{explanation.map((item, index) => <span key={index}>{typeof item === 'string' ? item : `${item.text || '已整理一条做法'}${item.source ? ` · 来源：${item.source}` : ''}`}</span>)}</div>}{conflicts.length > 0 && <div className="agent-conflicts"><strong>需要留意的冲突</strong>{conflicts.map((item, index) => <span key={index}>{typeof item === 'string' ? item : `${item.title || '规则冲突'}：${item.detail || item.choice || '请在下一轮对话里说明取舍'}`}</span>)}</div>}{error && <ErrorNotice message={error} />}<div className="agent-change-foot"><small>基于职能体 v{draft.base_version ?? version?.version ?? '—'} · 应用后只影响新任务</small><div><button className="cv-btn cv-btn-secondary" onClick={onChanged}>继续调整</button><button className="cv-btn cv-btn-primary" onClick={() => void apply()} disabled={busy || !draft.revision || conflicts.length > 0}>{busy ? '应用中…' : '应用更新'}</button></div></div></section>
}

function ModelSettings({ agent, version, draft, props, onChanged }: { agent: Agent; version?: AgentVersion; draft?: Draft | null; props: PageProps; onChanged: () => void }) {
  const settings = (draft?.patch?.model_settings || version?.model_settings || {}) as Record<string, unknown>; const fallback = settings.default as Record<string, unknown> | undefined; const [provider, setProvider] = useState(String(fallback?.provider || 'codex')); const [model, setModel] = useState(String(fallback?.model || settings.model || '')); const [stages, setStages] = useState<Record<string, string>>({ planning: '', execution: '', verification: '', maintenance: '' }); const [busy, setBusy] = useState(false); const [notice, setNotice] = useState(''); const [versions, setVersions] = useState<AgentVersion[]>([])
  useEffect(() => { const next = (draft?.patch?.model_settings || version?.model_settings || {}) as Record<string, unknown>; const def = next.default as Record<string, unknown> | undefined; setProvider(String(def?.provider || 'codex')); setModel(String(def?.model || next.model || '')); setStages(Object.fromEntries(['planning', 'execution', 'verification', 'maintenance'].map((key) => { const value = next[key] as Record<string, unknown> | undefined; return [key, String(value?.model || '')] }))) }, [draft?.revision, version?.version])
  useEffect(() => { void request<{ versions: AgentVersion[] }>(`${pathAgent(agent.id)}/versions`, { onUnauthorized: props.onUnauthorized }).then((value) => setVersions(value.versions || [])).catch(() => undefined) }, [agent.id, props.onUnauthorized, version?.version])
  const editable = Boolean(draft)
  const save = async () => { if (!draft) { setNotice('请先通过维护对话建立配置草稿。'); return } setBusy(true); setNotice(''); try { const modelSettings: Record<string, unknown> = { ...settings, default: { provider, model: model.trim() } }; for (const [key, value] of Object.entries(stages)) { if (value.trim()) modelSettings[key] = { provider: String((settings[key] as { provider?: string } | undefined)?.provider || provider), model: value.trim() }; else delete modelSettings[key] } await request(`${pathAgent(agent.id)}/draft`, { method: 'PATCH', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { expected_revision: draft.revision, patch: { model_settings: modelSettings } } }); setNotice('已保存到维护草稿，应用后对新任务生效。'); onChanged() } catch (cause) { setNotice(errorText(cause)) } finally { setBusy(false) } }
  const rollback = async (target: number) => { setBusy(true); setNotice(''); try { await request(`${pathAgent(agent.id)}/rollback`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { version: target } }); setNotice(`已回滚到 v${target}，只影响后续新任务。`); onChanged() } catch (cause) { setNotice(errorText(cause)) } finally { setBusy(false) } }
  return <div className="agent-settings-body"><label>服务商<select disabled={!editable} value={provider} onChange={(event) => setProvider(event.target.value)}><option value="codex">平台默认</option><option value="claude">Claude</option><option value="dsh">DSH</option></select></label><label>默认模型<input disabled={!editable} value={model} onChange={(event) => setModel(event.target.value)} placeholder="留空表示使用平台实际配置" /></label>{(['planning', 'execution', 'verification', 'maintenance'] as const).map((key) => <label key={key}>{key === 'planning' ? '规划阶段' : key === 'execution' ? '执行阶段' : key === 'verification' ? '验证阶段' : '维护对话'}<input disabled={!editable} value={stages[key]} onChange={(event) => setStages((current) => ({ ...current, [key]: event.target.value }))} placeholder="沿用默认模型" /></label>)}<small>阶段覆盖留空时继承默认模型；实际 provider/model 会在运行快照中记录。</small>{editable ? <button className="cv-btn cv-btn-secondary" onClick={() => void save()} disabled={busy || !draft}>{busy ? '保存中…' : '保存模型设置'}</button> : <p>打开维护对话建立草稿后可编辑模型配置。</p>}{versions.length > 1 && <label>恢复历史版本<select value={version?.version || ''} onChange={(event) => void rollback(Number(event.target.value))} disabled={busy || !editable}><option value="">选择版本</option>{versions.map((item) => <option key={item.version} value={item.version}>v{item.version}{item.version === version?.version ? '（当前）' : ''}</option>)}</select></label>}{notice && <span className="agent-settings-notice">{notice}</span>}</div>
}

// ---- Maintenance conversation mini-panel (embedded in mgmt page, not a second chat) ----

function MaintenancePanel({ agent, props, onChanged }: { agent: Agent; props: PageProps; onChanged: () => void }) {
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [version, setVersion] = useState<AgentVersion | undefined>(agent.version)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [maintenanceJob, setMaintenanceJob] = useState<MaintenanceJob | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError(null)
    try {
      const detail = await request<Agent>(pathAgent(agent.id), { signal, onUnauthorized: props.onUnauthorized }); setVersion(detail.version)
      const rows = asList<Conversation>(await request<unknown>(`${pathAgent(agent.id)}/conversations`, { signal, onUnauthorized: props.onUnauthorized }), 'conversations').filter((item) => item.mode === 'maintain')
      const current = rows[0] ? await request<Conversation>(pathConversation(rows[0].id), { signal, onUnauthorized: props.onUnauthorized }) : null; setConversation(current)
      const rawDraft = await request<Draft>(`${pathAgent(agent.id)}/draft`, { signal, onUnauthorized: props.onUnauthorized }).catch(() => null)
      setDraft(rawDraft && rawDraft.revision != null ? rawDraft : null)
      const jobMessages = current?.messages?.filter((message) => message.job_id) ?? []; const latestJob = jobMessages[jobMessages.length - 1]; const terminalJob = [...jobMessages].reverse().find((message) => ['completed', 'failed', 'cancelled', 'interrupted'].includes(message.status || '')); const activeJob = [...jobMessages].reverse().find((message) => ['pending', 'running', 'cancel_requested'].includes(message.status || '') && message.job_id === latestJob?.job_id); setMaintenanceJob(terminalJob && terminalJob.job_id === latestJob?.job_id ? { id: terminalJob.job_id as string, status: (terminalJob.status || 'completed') as MaintenanceJob['status'] } : activeJob?.job_id ? { id: activeJob.job_id, status: (activeJob.status || 'pending') as MaintenanceJob['status'] } : null)
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401) && !(cause instanceof DOMException && cause.name === 'AbortError')) setError(errorText(cause)) } finally { setLoading(false) }
  }, [agent.id, props.onUnauthorized])
  useEffect(() => { const c = new AbortController(); void load(c.signal); return () => c.abort() }, [load])

  useEffect(() => { if (!maintenanceJob || ['completed', 'failed', 'cancelled', 'interrupted'].includes(maintenanceJob.status)) return; const timer = window.setInterval(() => { void request<MaintenanceJob>(`${base}/maintenance-jobs/${encodeURIComponent(maintenanceJob.id)}`, { onUnauthorized: props.onUnauthorized }).then((next) => { setMaintenanceJob(next); if (['completed', 'failed', 'cancelled', 'interrupted'].includes(next.status)) { void request<Draft>(`${pathAgent(agent.id)}/draft`, { onUnauthorized: props.onUnauthorized }).then(setDraft).catch(() => undefined); if (conversation) void request<Conversation>(pathConversation(conversation.id), { onUnauthorized: props.onUnauthorized }).then(setConversation).catch(() => undefined) } }).catch(() => undefined) }, 1500); return () => window.clearInterval(timer) }, [maintenanceJob, props.onUnauthorized, agent.id, conversation])

  const createConversation = async () => { const result = await request<Conversation>(`${pathAgent(agent.id)}/conversations`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { mode: 'maintain' } }); setConversation(result); return result }

  const send = async (event: FormEvent) => {
    event.preventDefault(); if (!text.trim() || busy || Boolean(maintenanceJob && ['pending', 'running', 'cancel_requested'].includes(maintenanceJob.status))) return; setBusy(true); setError(null)
    try {
      let current = conversation; if (!current) current = await createConversation()
      const raw = await request<{ conversation: Conversation; draft?: Draft; job_id?: string; status?: MaintenanceJob['status'] }>(`${pathConversation(current.id)}/messages`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { content: text.trim() } })
      if (raw.conversation) setConversation(raw.conversation)
      if (raw.job_id) setMaintenanceJob({ id: raw.job_id, status: raw.status || 'pending' })
      if (raw.draft) setDraft(raw.draft as Draft)
      setText('')
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(false) }
  }

  const retryMaintenance = async () => { if (!conversation || busy) return; setBusy(true); setError(null); try { const result = await request<{ conversation: Conversation; job_id: string; status: MaintenanceJob['status'] }>(`${pathConversation(conversation.id)}/retry`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized }); setConversation(result.conversation); setMaintenanceJob({ id: result.job_id, status: result.status }) } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) } }
  const cancelJob = async () => { if (!maintenanceJob) return; setBusy(true); try { const next = await request<MaintenanceJob>(`${base}/maintenance-jobs/${encodeURIComponent(maintenanceJob.id)}/cancel`, { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized }); setMaintenanceJob(next) } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) } }

  const messages = conversation?.messages ?? []
  const completedJobs = new Set(messages.filter((m) => m.job_id && ['completed', 'failed', 'cancelled', 'interrupted'].includes(m.status || '')).map((m) => m.job_id))
  const visible = messages.filter((m) => !(m.status === 'pending' && m.job_id && completedJobs.has(m.job_id)))

  const handleChanged = () => { void load(); onChanged() }

  return (
    <div className="agent-maintain-section">
      <p className="agent-section-desc">上传 Skill、粘贴 Prompt 或描述经验，整理成草稿后点击"应用更新"成为新版本。</p>
      {error && <ErrorNotice message={error} />}
      {loading && !conversation && <p>正在加载维护对话…</p>}
      {!loading && visible.length > 0 && <div className="agent-maintain-messages">{visible.slice(-5).map((m, i) =>
        <div key={m.id || i} className={`agent-maintain-msg ${m.role === 'user' ? 'is-user' : ''}`}>
          <small>{m.role === 'user' ? '你' : '职能体'}{m.at || m.created_at ? ` · ${formatDate(m.at || m.created_at)}` : ''}</small>
          <span>{m.content.slice(0, 200)}{m.content.length > 200 ? '…' : ''}</span>
        </div>
      )}</div>}
      {maintenanceJob && <div className="agent-live-run"><span className="agent-live-dot" /><span>维护整理：{maintenanceJob.status === 'completed' ? '已完成' : maintenanceJob.status === 'failed' ? '失败' : maintenanceJob.status === 'cancelled' ? '已取消' : maintenanceJob.status === 'interrupted' ? '已中断，请重试' : '处理中'}</span>{['failed', 'cancelled', 'interrupted'].includes(maintenanceJob.status) && <button className="cv-btn cv-btn-secondary" disabled={busy} onClick={() => void retryMaintenance()}>重新整理</button>}{['pending', 'running', 'cancel_requested'].includes(maintenanceJob.status) && <button className="cv-btn cv-btn-secondary" onClick={() => void cancelJob()} disabled={busy || maintenanceJob.status === 'cancel_requested'}>取消</button>}</div>}
      {draft && draft.revision > 0 && <DraftCard draft={draft} version={version} props={props} agentId={agent.id} onChanged={handleChanged} />}
      <form className="agent-maintain-composer" onSubmit={send}>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} placeholder="例如：以后以客户当前版本为准，别升级所有依赖…" />
        <button className="cv-btn cv-btn-primary" disabled={busy || Boolean(maintenanceJob && ['pending', 'running', 'cancel_requested'].includes(maintenanceJob.status)) || !text.trim()}>{busy ? '处理中…' : '发送'}</button>
      </form>
      <ModelSettings agent={agent} version={version} draft={draft} props={props} onChanged={handleChanged} />
    </div>
  )
}

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
  const [bindNotice, setBindNotice] = useState('')

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
  /** Add a method module to THIS role's manifest, using the same manifest
   *  endpoint the 岗位清单 editor uses. Selecting here is the action; the user
   *  no longer has to go find the editor and repeat the choice. */
  const addModule = async (moduleId: string, moduleVersion: number) => {
    if (binding) return
    setBinding(moduleId); setBindError(''); setBindNotice('')
    const manifestPath = `/api/v4/agents/${encodeURIComponent(agentId)}/manifest`
    try {
      const current = await request<{ revision: number; identity: string; skills: Array<{ id: string; version: number }>; assertions: string[] }>(manifestPath, { onUnauthorized: props.onUnauthorized })
      if ((current.skills || []).some(sk => sk.id === moduleId)) { setBindError('这个方法已经在岗位清单里了'); return }
      await request(manifestPath, {
        method: 'PUT', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized,
        body: { revision: current.revision, identity: current.identity,
                skills: [...(current.skills || []), { id: moduleId, version: moduleVersion }],
                assertions: current.assertions || [] },
      })
      setBindNotice('已加入岗位清单，上方摘要已更新。')
      setEpoch(v => v + 1); onChanged()
    } catch (cause) { setBindError(errorText(cause)) } finally { setBinding('') }
  }

  const attachPack = async (packId: string) => {
    if (binding) return
    setBinding(packId); setBindError(''); setBindNotice('')
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
      setBindNotice('已挂靠，上方「已挂靠工具」已更新。')
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
      <p className="agent-section-desc">从以下来源为当前职能体添加能力。</p>
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
          <p>上传 Skill ZIP 文件添加到当前职能体。若要整包导入一个新职能体，请返回职能体列表使用「导入职能体」按钮。</p>
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
          <p>从团队已有的方法模块和沉淀能力中选择。模块可直接在岗位清单中引用；沉淀能力需要先升格为 Skill 才能加入。独立工具通过挂靠绑定。</p>
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
                      <button type="button" className="cv-btn cv-btn-secondary agent-attach-btn"
                        disabled={Boolean(binding)} onClick={() => void addModule(m.id, m.version)}>
                        {binding === m.id ? '加入中…' : '加入岗位清单'}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : <p>团队还没有方法模块。</p>}
              <p style={{ marginTop: 12 }}>加入后可在「编辑工作规范」里调整版本或移除。</p>

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
              <p className="agent-section-note">沉淀能力目前用于项目运行。要加入职能体，请在详情页点击「升格为 Skill」。</p>

              <h3 style={{ marginTop: 16 }}>可用工具</h3>
              {bindError && <ErrorNotice message={bindError} />}
              {bindNotice && <p role="status" className="agent-section-note">{bindNotice}</p>}
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
                    <p className="agent-section-note">这些还没有可用版本。完成验证和发布后即可挂靠。</p>
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
          <p>从开发运行中沉淀的工具包：选择成果 → 验证 → 发布 → 挂靠到当前职能体。</p>
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

function AttachedTools({ agentId, refreshKey = 0, ...props }: PageProps & { agentId: string; refreshKey?: number }) {
  const [bindings, setBindings] = useState<PackBinding[] | null>(null)
  const [error, setError] = useState('')
  const load = useCallback((signal?: AbortSignal) => {
    return request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`, { signal, onUnauthorized: props.onUnauthorized })
      .then(r => setBindings(r.bindings || []))
      .catch(e => { if (!signal?.aborted) setError(errorText(e)) })
    // refreshKey changes after an attach/upgrade/detach so this list re-reads the
    // real bindings instead of waiting for a full page reload.
  }, [agentId, refreshKey, props.onUnauthorized])
  useEffect(() => { const c = new AbortController(); void load(c.signal); return () => c.abort() }, [load])

  return (
    <section className="agent-section" aria-label="已挂靠工具">
      <h2>已挂靠工具</h2>
      <p className="agent-section-desc">已安装的独立工具。版本在挂靠时锁定，可在下方升级或解除。</p>
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
                {b.upgrade_available && <Link className="agent-tool-upgrade" to={packHref(b.pack_id, agentId, 'versions')}>可升级到 v{b.latest_version}</Link>}
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
                {(b.environment as { stale?: boolean; stale_reason?: string } | undefined)?.stale && <span className="agent-tool-stale">{(b.environment as { stale_reason?: string }).stale_reason || '上次检查结果已过期'}</span>}
              </div>
              <Link className="cv-btn cv-btn-secondary" style={{ fontSize: 13, padding: '6px 10px' }}
                to={packHref(b.pack_id, agentId)}>管理</Link>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ---- Management page (detail view) ----

function ManifestSummary({ agentId, refreshKey = 0, onUnauthorized }: { agentId: string; refreshKey?: number; onUnauthorized: () => void }) {
  const [summary, setSummary] = useState<{ identity: string; skillCount: number; assertionCount: number; revision: number } | null>(null)
  useEffect(() => {
    const c = new AbortController()
    void request<{ revision: number; identity: string; skills: unknown[]; assertions: string[] }>(`/api/v4/agents/${encodeURIComponent(agentId)}/manifest`, { signal: c.signal, onUnauthorized })
      .then(m => { if (!c.signal.aborted) setSummary({ identity: m.identity, skillCount: m.skills.length, assertionCount: m.assertions.length, revision: m.revision }) })
      .catch(() => undefined)
    return () => c.abort()
    // refreshKey advances after any action that changes the manifest -- adding a
    // method, saving the roster, restoring a version -- so the summary and the
    // editor never show different revisions.
  }, [agentId, refreshKey, onUnauthorized])
  if (!summary) return <p>正在读取规范…</p>
  return (
    <div className="agent-spec-summary">
      <p><strong>身份段：</strong>{summary.identity ? summary.identity.slice(0, 120) + (summary.identity.length > 120 ? '…' : '') : '尚未编写'}</p>
      <p>{summary.skillCount} 个 Skill · {summary.assertionCount} 项验收断言 · revision {summary.revision}</p>
    </div>
  )
}

function AgentManagementPage({ agent: initialAgent, props }: { agent: Agent; props: PageProps }) {
  const [agent, setAgent] = useState(initialAgent)
  const setWorkTitle = useWorkTitle()
  useEffect(() => { setWorkTitle(agent.name); return () => setWorkTitle(null) }, [agent.name, setWorkTitle])
  const [editingMeta, setEditingMeta] = useState(false)
  const [manifestEpoch, setManifestEpoch] = useState(0)
  // Bumped whenever the set of attached tools actually changed.
  const [toolEpoch, setToolEpoch] = useState(0)
  // Drives the work-spec summary. Separate from manifestEpoch so the editor is
  // not remounted while the user is typing in it.
  const [specEpoch, setSpecEpoch] = useState(0)
  const [showCreateProject, setShowCreateProject] = useState(false)
  const [projectNotice, setProjectNotice] = useState('')
  const isAdmin = props.user?.role !== 'member'
  const navigate = useNavigate()

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
        <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Link className="cv-btn cv-btn-primary" to={`/agents/${encodeURIComponent(agent.id)}/chat`}>
            开始对话 <Icon name="arrow" width={15} height={15} />
          </Link>
          {isAdmin && (
            <button className="cv-btn cv-btn-secondary" onClick={() => setShowCreateProject(v => !v)}>
              创建样例项目
            </button>
          )}
          {agent.builtin_pack && (
            <a className="cv-agent-dl"
              href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}>
              <Icon name="download" width={14} height={14} /> 导出职能包
            </a>
          )}
        </div>
        {showCreateProject && (
          <div style={{ marginTop: 12 }}>
            <ProjectForm {...props} agentId={agent.id} uploadFirst
              onCancel={() => setShowCreateProject(false)}
              onCreated={(project, warning) => {
                setShowCreateProject(false)
                setProjectNotice(warning || `项目「${project.name}」已创建并关联到本职能体。`)
                navigate(`/projects/${encodeURIComponent(String(project.id))}`)
              }} />
          </div>
        )}
        {projectNotice && <p role="status" style={{ marginTop: 8 }}>{projectNotice}</p>}
      </section>

      {/* ---- Section 2: Work Specs (default: summary; editing folded) ---- */}
      <section className="agent-section" aria-label="工作规范">
        <h2>工作规范</h2>
        <ManifestSummary agentId={agent.id} refreshKey={specEpoch} onUnauthorized={props.onUnauthorized} />
        <details>
          <summary><Icon name="triangle" className="wb-disclosure-icon" />编辑工作规范</summary>
          <AgentManifest key={`${agent.id}:${manifestEpoch}`} agentId={agent.id} {...props} onChanged={() => setSpecEpoch(v => v + 1)} />
          <AgentEvolution key={agent.id} agentId={agent.id} {...props} onChanged={() => { setManifestEpoch(v => v + 1); setSpecEpoch(v => v + 1); void refreshAgent() }} />
        </details>
      </section>

      {/* ---- Section 3: Attached Tools ---- */}
      <AttachedTools agentId={agent.id} refreshKey={toolEpoch} {...props} />

      {/* ---- Add Ability ---- */}
      {isAdmin && <AddAbilityPanel agentId={agent.id} {...props} onChanged={() => { setManifestEpoch(v => v + 1); setToolEpoch(v => v + 1); setSpecEpoch(v => v + 1); void refreshAgent() }} />}

      {/* ---- Advanced / Maintenance (collapsed by default) ---- */}
      {isAdmin && (
        <section className="agent-section" aria-label="高级设置">
          <details>
            <summary><h2 style={{ display: 'inline' }}>高级设置 / 维护</h2></summary>
            <p className="agent-section-desc">模型配置、维护对话（生成/应用草稿）、版本回退。</p>
            <MaintenancePanel agent={agent} props={props} onChanged={() => { setManifestEpoch(v => v + 1); void refreshAgent() }} />
          </details>
        </section>
      )}
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

  // One generation per requested agent. A response that arrives after the user
  // has moved on belongs to a page that is no longer shown, so it must not set
  // `selected` (which would bring the previous agent back as the editable one)
  // nor clear the loading flag for the request that is still in flight.
  const generation = useRef(0)
  const load = useCallback(() => {
    const mine = ++generation.current
    setLoading(true)
    void request<unknown>(`${base}/agents`, { onUnauthorized: props.onUnauthorized })
      .then((value) => {
        if (generation.current !== mine) return
        const list = asList<Agent>(value, 'agents')
        setAgents(list)
        const wanted = params.agentId ? list.find((item) => item.id === params.agentId) : undefined
        setSelected(wanted || null)
      })
      .catch((cause) => {
        if (generation.current !== mine) return
        if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause))
      })
      .finally(() => { if (generation.current === mine) setLoading(false) })
  }, [params.agentId, props.onUnauthorized])

  useEffect(() => { load() }, [load])

  // While a different agent is being fetched, the previously selected one is no
  // longer the page's subject: showing its editor would let a save land on the
  // agent the user just navigated away from.
  const stale = Boolean(params.agentId && selected && selected.id !== params.agentId)

  const choose = (id: string) => navigate(`/agents/${encodeURIComponent(id)}`)

  // Detail view: management page for a specific agent (no left sidebar)
  if (params.agentId) {
    return (
      <div className="wb-page agent-page">
        {(loading || stale) && (!selected || stale) && <div className="wb-card agent-loading">正在读取职能体…</div>}
        {!loading && !selected && !error && <ErrorNotice message="未找到这个职能体，请返回列表选择。" />}
        {error && <ErrorNotice message={error} />}
        {/* Keyed by id: navigating a1 -> a2 inside one route tree must rebuild the
            management state, otherwise edits and bindings keep writing to the
            agent that was mounted first. */}
        {selected && !stale && <AgentManagementPage key={selected.id} agent={selected} props={props} />}
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
