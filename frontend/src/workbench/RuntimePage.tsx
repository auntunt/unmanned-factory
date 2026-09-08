import { useCallback, useEffect, useMemo, useState } from 'react'
import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, formatDate, PageHeader, errorText, type PageProps } from './ui'
import { RUNTIME_PROVIDERS, RUNTIME_ROLES, isRuntimeRole, runtimeProviderLabel, runtimeRoleLabel, type RuntimeConfiguration, type RuntimeInspection, type RuntimeLimits, type RuntimeProbe, type RuntimeProfiles, type RuntimeRole } from './runtime-types'
import './detail.css'

const blankProfiles: RuntimeProfiles = {
  planner: { provider: '', model: '' },
  cheap: { provider: '', model: '' },
  standard: { provider: '', model: '' },
  strong: { provider: '', model: '' },
}

const defaultLimits: RuntimeLimits = { timeout_s: 600, max_parallel: 2, max_tasks: 20, unknown_cost_policy: 'stop' }
const roleIds: Record<RuntimeRole, string> = { planner: 'planner', cheap: 'cheap', standard: 'standard', strong: 'strong' }

function emptyConfiguration(): RuntimeConfiguration {
  return { revision: 0, profiles: { ...blankProfiles }, limits: { ...defaultLimits }, updated_at: null }
}

function normalizeConfiguration(value: Partial<RuntimeInspection> | null | undefined): RuntimeInspection {
  const sourceProfiles = (value?.profiles ?? {}) as Partial<RuntimeProfiles>
  const profiles = RUNTIME_ROLES.reduce((all, role) => {
    const profile = sourceProfiles[role]
    all[role] = { provider: typeof profile?.provider === 'string' ? profile.provider : '', model: typeof profile?.model === 'string' ? profile.model : '' }
    return all
  }, { ...blankProfiles } as RuntimeProfiles)
  const sourceLimits = (value?.limits ?? {}) as Partial<RuntimeLimits>
  const configuration: RuntimeInspection = {
    ...emptyConfiguration(),
    ...value,
    revision: typeof value?.revision === 'number' ? value.revision : typeof value?.configuration_revision === 'number' ? value.configuration_revision : 0,
    profiles,
    limits: {
      timeout_s: typeof sourceLimits.timeout_s === 'number' ? sourceLimits.timeout_s : defaultLimits.timeout_s,
      max_parallel: typeof sourceLimits.max_parallel === 'number' ? sourceLimits.max_parallel : defaultLimits.max_parallel,
      max_tasks: typeof sourceLimits.max_tasks === 'number' ? sourceLimits.max_tasks : defaultLimits.max_tasks,
      unknown_cost_policy: sourceLimits.unknown_cost_policy === 'allow_bounded' ? 'allow_bounded' : 'stop',
    },
    tools: Array.isArray(value?.tools) ? value.tools : [],
    blockers: Array.isArray(value?.blockers) ? value.blockers : [],
    last_probes: Array.isArray(value?.last_probes) ? value.last_probes : [],
  }
  return configuration
}

function probeFor(probes: RuntimeProbe[], role: RuntimeRole, revision: number): RuntimeProbe | undefined {
  return probes.filter((probe) => probe.profile === role && probe.configuration_revision === revision).sort((a, b) => String(b.checked_at).localeCompare(String(a.checked_at)))[0]
}

function validateDraft(draft: RuntimeConfiguration): string | null {
  for (const role of RUNTIME_ROLES) {
    if (!draft.profiles[role].provider || !draft.profiles[role].model.trim()) return `请完整填写“${runtimeRoleLabel(role)}”配置。`
  }
  if (draft.profiles.planner.provider === 'dsh') return 'DSH 不能作为规划 provider。'
  if (draft.limits.timeout_s < 30 || draft.limits.timeout_s > 1800) return '超时必须在 30–1800 秒之间。'
  if (draft.limits.max_parallel < 1 || draft.limits.max_parallel > 4) return '最大并行数必须在 1–4 之间。'
  if (draft.limits.max_tasks < 1 || draft.limits.max_tasks > 20) return '最大任务数必须在 1–20 之间。'
  return null
}

function ToolDiagnostics({ inspection }: { inspection: RuntimeInspection }) {
  return <section className="wb-detail-card wb-runtime-section" aria-labelledby="runtime-tools-title"><h2 id="runtime-tools-title">运行环境检查</h2><p className="wb-runtime-note">读取诊断只检查已安装包、导入兼容性和安全的授权提示，不会调用模型，也不会读取凭据内容。已安装不等于 provider 已连接。</p>{inspection.tools.length === 0 ? <div className="wb-empty-inline">服务端尚未返回工具诊断。</div> : <div className="wb-tools">{inspection.tools.map((tool) => { const installed = tool.installed; const importStatus = tool.import_status ?? 'unknown'; const auth = tool.auth ?? 'unknown'; const localStatus = tool.runtime_status ?? (installed ? '本地可用' : '不可用'); return <article className="wb-tool" key={tool.id}><div className="wb-tool-head"><div><div className="wb-tool-name">{tool.id}</div><div className="wb-tool-detail">版本：{tool.version || '—'} · 导入兼容性：{importStatus} · 授权提示：{auth === 'present' ? '已检测到来源' : auth === 'missing' ? '未检测到来源' : '未知'} · 本地状态：{localStatus}</div></div><span className={`wb-tool-status ${installed ? '' : 'wb-tool-status-missing'}`}>{installed ? '已安装' : '未安装'}</span></div>{tool.issues && tool.issues.length > 0 && <ul className="wb-tool-issues">{tool.issues.map((issue) => <li key={`${issue.code}-${issue.message}`}>{issue.message}</li>)}</ul>}</article> })}</div>}{inspection.auth_sources && inspection.auth_sources.length > 0 && <div className="wb-auth-sources"><strong>授权来源提示</strong><span>{inspection.auth_sources.map((source) => typeof source === 'string' ? source : source.source || source.id).join('、')}</span></div>}{inspection.host && <div className="wb-detail-meta"><span>Python：<strong>{inspection.host.python_version || '—'}</strong></span><span>Git：<strong>{inspection.host.git_available ? '可用' : '不可用'}</strong></span><span>Node：<strong>{inspection.host.node_available ? '可用' : '不可用'}</strong></span><span>前端构建：<strong>{inspection.host.frontend_built ? '已完成' : '未完成'}</strong></span></div>}{inspection.verification_note && <p className="wb-runtime-note">{inspection.verification_note}</p>}</section>
}

function ReadinessSummary({ inspection }: { inspection: RuntimeInspection }) {
  const local = inspection.local_readiness
  const live = inspection.readiness
  const rows = [
    ['规划', local?.planning, live?.planning],
    ['执行', local?.execution, live?.execution],
    ['发布', local?.publishing, live?.publishing],
  ] as const
  return <section className="wb-detail-card wb-runtime-section" aria-labelledby="runtime-readiness-title"><h2 id="runtime-readiness-title">可用性状态</h2><p className="wb-runtime-note">本地可用性与 provider 的实时探针验证分开显示。未运行探针不会显示为已连接。</p><div className="wb-readiness-list">{rows.map(([label, localReady, verified]) => <div className="wb-side-stat" key={label}><span>{label}</span><span>{localReady === undefined ? '本地状态未知' : localReady ? '本地可用' : '本地受阻'} · {verified === undefined ? '验证状态未知' : verified ? '已探测通过' : '尚未验证/不可用'}</span></div>)}</div>{inspection.live_verified && <div className="wb-detail-meta"><span>角色探针状态仅代表当前配置修订</span></div>}</section>
}

function LiveProbes({ inspection, probing, unsaved, onProbe }: { inspection: RuntimeInspection; probing: RuntimeRole | null; unsaved: boolean; onProbe: (role: RuntimeRole) => void }) {
  return <section className="wb-detail-card wb-runtime-section" aria-labelledby="runtime-probe-title"><h2 id="runtime-probe-title">实时连接探针</h2><p className="wb-runtime-note">探针会向所选 provider 发送固定的只读短请求，可能产生费用。只有点击“运行探针”才会发起请求；页面读取和保存设置都不会自动探测。</p>{unsaved && <div className="wb-notice">配置有未保存改动。请先保存设置，再对已保存的 provider 运行探针。</div>}<div className="wb-probes">{RUNTIME_ROLES.map((role) => { const profile = inspection.profiles[role]; const probe = probeFor(inspection.last_probes, role, inspection.revision); const unsupported = profile.provider === 'dsh'; const disabled = unsupported || !profile.provider || !profile.model || probing !== null || unsaved; return <article className={`wb-probe ${probe ? `wb-probe-${probe.outcome}` : ''}`} key={role}><div><strong>{runtimeRoleLabel(role)}</strong><div className="wb-probe-meta">{profile.provider ? `${runtimeProviderLabel(profile.provider)} · ${profile.model || '未填写模型'}` : '尚未配置 provider'}</div>{probe && <div className="wb-probe-message">{probe.outcome === 'passed' ? '探针通过' : probe.outcome === 'unsupported' ? '当前 provider 不支持探针' : '探针失败'} · {probe.message} · {formatDate(probe.checked_at)}</div>}</div><button className="wb-probe-button" disabled={disabled} title={unsupported ? 'DSH 当前不支持实时探针' : unsaved ? '请先保存配置' : undefined} onClick={() => onProbe(role)}>{probing === role ? '探测中…' : unsupported ? 'DSH 不支持探针' : '运行探针（可能计费）'}</button></article> })}</div></section>
}

export default function RuntimePage({ csrfToken, onUnauthorized }: PageProps) {
  const [inspection, setInspection] = useState<RuntimeInspection | null>(null)
  const [draft, setDraft] = useState<RuntimeConfiguration>(emptyConfiguration())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [probing, setProbing] = useState<RuntimeRole | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError(null)
    try { const next = await request<RuntimeInspection>('/api/v2/runtime', { onUnauthorized, signal }); if (!signal?.aborted) { const normalized = normalizeConfiguration(next); setInspection(normalized); setDraft({ revision: normalized.revision, profiles: normalized.profiles, limits: normalized.limits, updated_at: normalized.updated_at }) } } catch (cause) { if (!signal?.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { if (!signal?.aborted) setLoading(false) }
  }, [onUnauthorized])

  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort() }, [load])

  const updateProfile = (role: RuntimeRole, field: 'provider' | 'model', value: string) => setDraft((current) => ({ ...current, profiles: { ...current.profiles, [role]: { ...current.profiles[role], [field]: value } } }))
  const updateLimit = (field: keyof RuntimeLimits, value: string) => setDraft((current) => ({ ...current, limits: { ...current.limits, [field]: field === 'unknown_cost_policy' ? value : Number(value) } as RuntimeLimits }))

  const save = async () => {
    const validation = validateDraft(draft)
    if (validation) { setError(validation); return }
    setSaving(true); setError(null); setNotice(null)
    try { const next = await request<RuntimeConfiguration>('/api/v2/runtime/profiles', { method: 'PUT', csrfToken, onUnauthorized, body: { revision: draft.revision, profiles: draft.profiles, limits: draft.limits } }); const normalized = normalizeConfiguration(next); setInspection((current) => current ? { ...current, ...normalized, tools: current.tools, host: current.host, blockers: current.blockers, last_probes: current.last_probes } : normalized); setDraft({ revision: normalized.revision, profiles: normalized.profiles, limits: normalized.limits, updated_at: normalized.updated_at }); setNotice('设置已保存。新的运行会使用这份配置，已经在规划或执行中的运行继续使用其冻结配置。'); void load() } catch (cause) { if (cause instanceof WorkspaceApiError && cause.status === 409) { const message = `设置版本已变化：${cause.detail} 请重新读取后再保存。`; await load(); setError(message) } else if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setSaving(false) }
  }

  const probe = async (role: RuntimeRole) => {
    if (!inspection || inspection.profiles[role].provider === 'dsh') return
    setProbing(role); setError(null); setNotice(null)
    try { const result = await request<RuntimeProbe>('/api/v2/runtime/probe', { method: 'POST', csrfToken, onUnauthorized, body: { profile: role, configuration_revision: inspection.revision } }); setInspection((current) => current ? { ...current, last_probes: [...current.last_probes.filter((item) => !(item.profile === role && item.configuration_revision === result.configuration_revision)), result] } : current); setNotice(result.outcome === 'passed' ? `${runtimeRoleLabel(role)}探针通过。` : `${runtimeRoleLabel(role)}探针结果：${result.message}`) } catch (cause) { if (cause instanceof WorkspaceApiError && cause.status === 409) { const message = `探针使用的配置版本已过期：${cause.detail} 请重新读取后再试。`; await load(); setError(message) } else if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setProbing(null) }
  }

  const hasBlockers = useMemo(() => Boolean(inspection?.blockers.length), [inspection?.blockers.length])
  const unsaved = useMemo(() => inspection ? JSON.stringify({ profiles: draft.profiles, limits: draft.limits }) !== JSON.stringify({ profiles: inspection.profiles, limits: inspection.limits }) : false, [draft.limits, draft.profiles, inspection])
  if (loading && !inspection) return <main className="wb-detail-page"><div className="wb-loading" role="status">正在读取运行配置与环境诊断…</div></main>
  if (!inspection) return <main className="wb-detail-page">{error ? <ErrorNotice message={error} /> : <EmptyState title="运行配置暂不可用" description="服务端没有返回运行配置。请稍后重试。" />}</main>
  return <main className="wb-detail-page"><PageHeader title="运行配置" description="配置模型角色、任务边界和执行前的环境检查。读取诊断不会发起付费请求。" actions={<button className="wb-button wb-button-primary" disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存设置'}</button>} />{error && <ErrorNotice message={error} />}{notice && <div className="wb-notice" role="status">{notice}</div>}<div className="wb-runtime-grid"><div><section className="wb-detail-card" aria-labelledby="profiles-title"><h2 id="profiles-title">模型角色</h2><p className="wb-runtime-note">四个角色必须分别配置。provider 与模型 ID 来自你的已安装账户，页面不会替你猜测有效模型。</p><div className="wb-profile-grid">{RUNTIME_ROLES.map((role) => { const profile = draft.profiles[role]; return <div className="wb-profile-row" key={role}><label htmlFor={`runtime-profile-${role}-provider`}>{runtimeRoleLabel(role)}</label><select id={`runtime-profile-${role}-provider`} aria-label={`${runtimeRoleLabel(role)} provider`} value={profile.provider} onChange={(event) => updateProfile(role, 'provider', event.target.value)}><option value="">选择 provider</option>{RUNTIME_PROVIDERS.map((provider) => <option value={provider} disabled={role === 'planner' && provider === 'dsh'} key={provider}>{runtimeProviderLabel(provider)}{role === 'planner' && provider === 'dsh' ? '（规划不可用）' : ''}</option>)}</select><input id={`runtime-profile-${role}-model`} aria-label={`${runtimeRoleLabel(role)}模型 ID`} value={profile.model} maxLength={160} onChange={(event) => updateProfile(role, 'model', event.target.value)} placeholder="填写账户中的模型 ID" /></div> })}</div></section><section className="wb-detail-card wb-runtime-section" aria-labelledby="limits-title"><h2 id="limits-title">执行边界</h2><p className="wb-runtime-note">未知费用默认停止；允许有界继续时仍受任务数、并行数和超时限制，费用可能仍未知，也不是美元预算封顶；系统会阻止自动发布。</p><div className="wb-runtime-limits"><div className="wb-field"><label htmlFor="runtime-timeout-s">单阶段总超时（秒）</label><input id="runtime-timeout-s" type="number" min={30} max={1800} value={draft.limits.timeout_s} onChange={(event) => updateLimit('timeout_s', event.target.value)} /><span className="wb-runtime-note">规划与执行阶段各自受此时限约束。</span></div><div className="wb-field"><label htmlFor="runtime-max-parallel">最大并行任务</label><input id="runtime-max-parallel" type="number" min={1} max={4} value={draft.limits.max_parallel} onChange={(event) => updateLimit('max_parallel', event.target.value)} /></div><div className="wb-field"><label htmlFor="runtime-max-tasks">单次最大任务数</label><input id="runtime-max-tasks" type="number" min={1} max={20} value={draft.limits.max_tasks} onChange={(event) => updateLimit('max_tasks', event.target.value)} /></div><div className="wb-field"><label htmlFor="runtime-unknown-cost-policy">未知费用策略</label><select id="runtime-unknown-cost-policy" value={draft.limits.unknown_cost_policy} onChange={(event) => updateLimit('unknown_cost_policy', event.target.value)}><option value="stop">停止并等待确认（推荐）</option><option value="allow_bounded">允许有界继续</option></select></div></div></section></div><div><ToolDiagnostics inspection={inspection} /><ReadinessSummary inspection={inspection} />{hasBlockers && <section className="wb-detail-card wb-runtime-section"><h2>当前阻塞项</h2><ul className="wb-tool-issues">{inspection.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></section>}<LiveProbes inspection={inspection} unsaved={unsaved} probing={probing} onProbe={(role) => void probe(role)} /></div></div></main>
}

export { normalizeConfiguration, validateDraft, probeFor, roleIds, isRuntimeRole }
