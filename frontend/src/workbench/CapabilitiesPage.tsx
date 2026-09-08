import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, formatDate, PageHeader, errorText, type PageProps } from './ui'
import type { Capability, CapabilityBinding, CapabilityDetail, V3Project } from './v3-types'
import './capability-library.css'

const categories = ['engineering', 'operations', 'collaboration', 'integration', 'migration', 'custom']
const categoryLabel: Record<string, string> = { engineering: '开发与修复', operations: '维护与部署', collaboration: '项目汇报', integration: 'API 与系统连接', migration: '迁移', custom: '其他工作' }
const categoryHint: Record<string, string> = { engineering: '代码开发、缺陷修复和检查', operations: '维护、部署和运行整理', collaboration: '进度、风险和项目汇报', integration: '在授权范围内连接 API', migration: '受控迁移和回退核验', custom: '你定义的工作方式' }
type Draft = Omit<Capability, 'id' | 'revision' | 'created_at' | 'updated_at' | 'source_run_id'>
const emptyDraft: Draft = { name: '', description: '', category: 'engineering', instructions: '', input_description: '', output_description: '', acceptance: [], status: 'draft' }

function isTemplate(capability: Capability): boolean {
  return capability.id.startsWith('template-')
}

function StatusPill({ status }: { status: string }) {
  return <span className={`cl-status cl-status-${status === 'ready' ? 'ready' : 'draft'}`}>{status === 'ready' ? '可直接使用' : '待完善'}</span>
}

function EmptyMessage({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="cl-empty"><span className="cl-empty-mark" aria-hidden="true">○</span><strong>{title}</strong><p>{description}</p>{action && <div>{action}</div>}</div>
}

function CapabilityForm({ capability, csrfToken, onUnauthorized, onSaved, onCancel }: PageProps & { capability?: Capability; onSaved: (value: Capability) => void; onCancel?: () => void }) {
  const [draft, setDraft] = useState<Draft>(capability ? { name: capability.name, description: capability.description, category: capability.category, instructions: capability.instructions, input_description: capability.input_description, output_description: capability.output_description, acceptance: capability.acceptance, status: capability.status } : emptyDraft)
  const [acceptanceText, setAcceptanceText] = useState(capability?.acceptance.join('\n') ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const update = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(null)
    const acceptance = acceptanceText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
    if (draft.status === 'ready' && acceptance.length === 0) { setError('切换为可直接使用前，至少写一条验收条件。'); return }
    setBusy(true)
    try {
      const payload = { ...draft, acceptance }
      const value = capability
        ? await request<Capability>(`/api/v3/capabilities/${encodeURIComponent(capability.id)}`, { method: 'PUT', csrfToken, onUnauthorized, body: { ...payload, expected_revision: capability.revision } })
        : await request<Capability>('/api/v3/capabilities', { method: 'POST', csrfToken, onUnauthorized, body: payload })
      onSaved(value)
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <form className="cl-form" onSubmit={save} aria-label={capability ? '编辑工作能力' : '创建工作能力'}>
    <div className="cl-form-head"><div><span className="cl-kicker">{capability ? `新版本 · v${capability.revision}` : '新工作能力'}</span><h3>{capability ? '更新工作说明' : '保存一项常做的工作'}</h3><p>写清楚工作边界，下一次就能从项目直接开始。</p></div>{onCancel && <button type="button" className="cl-icon-button" onClick={onCancel} aria-label="关闭编辑">×</button>}</div>
    <div className="cl-form-grid">
      <label>工作名称<input required maxLength={120} value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder="例如：项目维护" /></label>
      <label>工作分类<select value={draft.category} onChange={(event) => update('category', event.target.value)}>{categories.map((item) => <option key={item} value={item}>{categoryLabel[item]}</option>)}</select><small>{categoryHint[draft.category]}</small></label>
      <label className="cl-span-two">什么时候用<textarea required maxLength={4000} rows={3} value={draft.description} onChange={(event) => update('description', event.target.value)} placeholder="描述适合交给这项工作能力的场景和目标。" /></label>
      <label className="cl-span-two">需要提供什么<textarea required maxLength={4000} rows={3} value={draft.input_description} onChange={(event) => update('input_description', event.target.value)} placeholder="例如：项目、目标、范围、限制和已有资料。" /></label>
      <label className="cl-span-two">会交付什么<textarea required maxLength={4000} rows={3} value={draft.output_description} onChange={(event) => update('output_description', event.target.value)} placeholder="例如：修改结果、检查证据、风险和后续事项。" /></label>
    </div>
    <details className="cl-technical"><summary>管理与配置：工作步骤、验收条件</summary><div className="cl-technical-body"><label>工作步骤与边界<textarea required maxLength={12000} rows={6} value={draft.instructions} onChange={(event) => update('instructions', event.target.value)} placeholder="说明执行顺序、边界和允许使用的工具。" /></label><label>验收条件<textarea required={draft.status === 'ready'} rows={4} value={acceptanceText} onChange={(event) => setAcceptanceText(event.target.value)} placeholder="每行一条；可直接使用时至少写一条" /></label><label>使用状态<select value={draft.status} onChange={(event) => update('status', event.target.value)}><option value="draft">待完善</option><option value="ready">可直接使用</option></select></label></div></details>
    {error && <ErrorNotice message={error} />}
    <div className="cl-form-actions">{onCancel && <button type="button" className="cl-button cl-button-secondary" onClick={onCancel}>取消</button>}<button className="cl-button cl-button-primary" disabled={busy}>{busy ? '保存中…' : capability ? '保存新版本' : '保存工作能力'}</button></div>
  </form>
}

function InvokePanel({ capability, projects, projectsError, projectsLoading, csrfToken, onUnauthorized, isAdmin }: PageProps & { capability: Capability; projects: V3Project[]; projectsError: string | null; projectsLoading: boolean; isAdmin: boolean }) {
  const [projectId, setProjectId] = useState('')
  const [requestText, setRequestText] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<ReactNode>(null)
  const [error, setError] = useState<string | null>(null)
  const [bindings, setBindings] = useState<Record<string, CapabilityBinding[]>>({})
  const [bindingLoading, setBindingLoading] = useState(false)
  useEffect(() => {
    const controller = new AbortController(); setBindingLoading(true); setError(null)
    void Promise.allSettled(projects.map((project) => request<{ bindings: CapabilityBinding[] }>(`/api/v3/projects/${encodeURIComponent(String(project.id))}/capabilities`, { onUnauthorized, signal: controller.signal }).then((value) => ({ id: String(project.id), bindings: value.bindings })))).then((results) => {
      if (controller.signal.aborted) return
      const next: Record<string, CapabilityBinding[]> = {}; let failures = 0
      results.forEach((result) => { if (result.status === 'fulfilled') next[result.value.id] = result.value.bindings; else failures += 1 })
      setBindings(next)
      if (failures > 0) setError(`${failures} 个项目的绑定读取失败，其他项目仍可选择；提交前会再次确认绑定。`)
    }).finally(() => { if (!controller.signal.aborted) setBindingLoading(false) })
    return () => controller.abort()
  }, [onUnauthorized, projects])
  const bindingForProject = projectId ? bindings[projectId]?.find((binding) => binding.capability_id === capability.id) : undefined
  const invoke = async (event: FormEvent) => {
    event.preventDefault(); if (!projectId || requestText.trim().length < 5) return
    setBusy(true); setError(null); setNotice(null)
    try {
      let binding = bindingForProject
      if (!binding || binding.revision !== capability.revision) {
        if (!isAdmin) { setError('请管理员为项目启用该能力的当前版本。'); return }
        binding = await request<CapabilityBinding>(`/api/v3/projects/${encodeURIComponent(projectId)}/capabilities`, { method: 'POST', csrfToken, onUnauthorized, body: { capability_id: capability.id, revision: capability.revision } })
        setBindings((current) => ({ ...current, [projectId]: [...(current[projectId] ?? []).filter((item) => item.capability_id !== capability.id), binding as CapabilityBinding] }))
      }
      const run = await request<{ id: string }>(`/api/v3/capabilities/${encodeURIComponent(capability.id)}/invoke`, { method: 'POST', csrfToken, onUnauthorized, body: { project_id: projectId, revision: capability.revision, request: requestText.trim() } })
      setNotice(<span>已开始工作。<Link className="cl-link" to={`/runs/${encodeURIComponent(String(run.id))}`}>打开运行详情 →</Link></span>); setRequestText('')
    } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(false) }
  }
  if (capability.status !== 'ready') return <div className="cl-draft-note"><strong>这项工作还在待完善</strong><span>{isTemplate(capability) ? '这是内置模板，不是已经验证的 Agent。请在“管理与配置”中补充工作步骤和验收条件。' : '补充工作步骤和验收条件，并切换为“可直接使用”后，才能从项目开始。'}</span></div>
  return <form className="cl-invoke" onSubmit={invoke}>
    <div className="cl-invoke-head"><div><span className="cl-kicker">开始一项工作 · v{capability.revision}</span><h3>选择项目，描述这次目标</h3><p>系统会使用这个固定版本执行；管理员已启用的版本才可开始工作。</p></div></div>
    {projectsError && <ErrorNotice message={projectsError} />}{error && <ErrorNotice message={error} />}{bindingLoading && <div className="cl-loading-line">正在读取项目绑定…</div>}
    {!bindingLoading && projectsLoading && <div className="cl-loading-line">正在读取你可以使用的项目…</div>}
    {!bindingLoading && !projectsLoading && projects.length === 0 && !projectsError && <div className="cl-draft-note"><strong>{isAdmin ? '还没有项目可选' : '还没有分配给你的项目'}</strong><span>{isAdmin ? '先登记一个项目，再从这里开始工作。' : '请管理员为你分配项目后，再从这里开始工作。'}</span>{isAdmin && <Link className="cl-link" to="/projects">去登记项目 →</Link>}</div>}
    {!bindingLoading && !projectsLoading && projects.length === 0 && projectsError && <div className="cl-draft-note"><strong>项目列表暂时不可用</strong><span>修复项目读取后，再回来开始这项工作。</span><Link className="cl-link" to="/projects">打开项目页 →</Link></div>}
    {!bindingLoading && projects.length > 0 && <div className="cl-invoke-grid"><label>第一步 · 选择项目<select required value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">选择项目</option>{projects.map((project) => { const binding = bindings[String(project.id)]?.find((item) => item.capability_id === capability.id); const exact = binding?.revision === capability.revision; return <option key={String(project.id)} value={String(project.id)}>{project.name}{exact ? ' · 已绑定此版本' : ` · 将绑定 v${capability.revision}`}</option> })}</select></label><label>第二步 · 描述这次目标<textarea required minLength={5} maxLength={50000} rows={4} value={requestText} onChange={(event) => setRequestText(event.target.value)} placeholder={capability.input_description || '告诉系统这次要完成什么。'} /></label></div>}
    {projectId && !bindingForProject && <div className="cl-binding-note">{isAdmin ? `提交时会先绑定工作能力 v${capability.revision}，再创建运行。` : '请管理员为项目启用该能力后，再开始工作。'}</div>}{notice && <div className="cl-notice" role="status">{notice}</div>}
    {projects.length > 0 && <button className="cl-button cl-button-primary cl-start-button" disabled={busy || bindingLoading || projectsLoading || !projectId || requestText.trim().length < 5 || (!isAdmin && bindingForProject?.revision !== capability.revision)}>{busy ? '正在开始…' : bindingForProject?.revision === capability.revision ? '开始工作' : isAdmin ? '绑定此版本并开始' : '等待管理员启用'}</button>}
  </form>
}

function CapabilityCard({ capability, selected, onSelect }: { capability: Capability; selected: boolean; onSelect: () => void }) {
  return <button type="button" className={`cl-capability-card ${selected ? 'is-selected' : ''}`} onClick={onSelect} aria-pressed={selected}>
    <div className="cl-card-top"><StatusPill status={capability.status} /><span className="cl-card-category">{categoryLabel[capability.category] ?? capability.category} · {isTemplate(capability) ? '内置模板' : '自建'}</span></div>
    <strong>{capability.name}</strong><p>{capability.description || categoryHint[capability.category] || '保存一项可复用的工作方式。'}</p>
    <small>v{capability.revision} · 更新于 {formatDate(capability.updated_at)}</small><span className="cl-card-arrow" aria-hidden="true">→</span>
  </button>
}

export default function CapabilitiesPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [capabilities, setCapabilities] = useState<Capability[] | null>(null)
  const [selected, setSelected] = useState<CapabilityDetail | null>(null)
  const [projects, setProjects] = useState<V3Project[]>([])
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [memberProjectIds, setMemberProjectIds] = useState<Set<string> | null>(null)
  const [memberProjectsError, setMemberProjectsError] = useState<string | null>(null)
  const [showNew, setShowNew] = useState(false)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const listSequence = useRef(0); const detailSequence = useRef(0); const detailController = useRef<AbortController | null>(null)
  const isAdmin = user?.role !== 'member'
  const load = () => {
    const controller = new AbortController(); const sequence = ++listSequence.current; setError(null); setProjectsError(null)
    void Promise.allSettled([request<{ capabilities: Capability[] }>('/api/v3/capabilities', { onUnauthorized, signal: controller.signal }), request<{ projects: V3Project[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal })]).then(([capabilityResult, projectsResult]) => {
      if (controller.signal.aborted || sequence !== listSequence.current) return
      if (capabilityResult.status === 'fulfilled') setCapabilities(capabilityResult.value.capabilities); else setError(errorText(capabilityResult.reason))
      if (projectsResult.status === 'fulfilled') setProjects(projectsResult.value.projects); else setProjectsError(`项目列表加载失败：${errorText(projectsResult.reason)}`)
    })
    return controller
  }
  useEffect(() => { const controller = load(); return () => controller.abort() }, [onUnauthorized])
  useEffect(() => {
    if (isAdmin) { setMemberProjectIds(null); setMemberProjectsError(null); return }
    const controller = new AbortController(); setMemberProjectIds(null); setMemberProjectsError(null)
    void request<{ projects: Array<{ id: string | number; member_ids?: Array<string | number> }> }>('/api/v3/team', { onUnauthorized, signal: controller.signal }).then((payload) => { if (!controller.signal.aborted) setMemberProjectIds(new Set(payload.projects.filter((project) => (project.member_ids ?? []).some((id) => String(id) === String(user?.id))).map((project) => String(project.id)))) }).catch((cause) => { if (!controller.signal.aborted) setMemberProjectsError(errorText(cause)) })
    return () => controller.abort()
  }, [isAdmin, onUnauthorized, user?.id])
  const visible = useMemo(() => (capabilities ?? []).filter((item) => `${item.name} ${item.description} ${categoryLabel[item.category] ?? item.category}`.toLowerCase().includes(query.toLowerCase())), [capabilities, query])
  const ready = visible.filter((item) => item.status === 'ready')
  const drafts = visible.filter((item) => item.status !== 'ready')
  const authorizedProjects = isAdmin ? projects : memberProjectIds ? projects.filter((project) => memberProjectIds.has(String(project.id))) : []
  const projectsLoading = !isAdmin && memberProjectIds === null && !memberProjectsError
  const open = async (capability: Capability) => {
    detailController.current?.abort(); const controller = new AbortController(); detailController.current = controller; const sequence = ++detailSequence.current
    try { const next = await request<CapabilityDetail>(`/api/v3/capabilities/${encodeURIComponent(capability.id)}`, { onUnauthorized, signal: controller.signal }); if (!controller.signal.aborted && sequence === detailSequence.current) setSelected(next) } catch (cause) { if (!controller.signal.aborted && sequence === detailSequence.current) setError(errorText(cause)) }
  }
  useEffect(() => { const selectedId = searchParams.get('selected'); if (!selectedId) { detailController.current?.abort(); setSelected(null); return } if (!capabilities || selected?.id === selectedId) return; const item = capabilities.find((capability) => capability.id === selectedId); if (item) void open(item) }, [capabilities, searchParams, selected])
  useEffect(() => () => detailController.current?.abort(), [])
  const saveCreated = (value: Capability) => { setShowNew(false); setCapabilities((items) => items ? [value, ...items] : [value]); setSearchParams({ selected: value.id }, { replace: true }) }
  return <div className="cl-page">
    <PageHeader title="工作能力" description="把常做的工作保存下来，下次选择项目即可使用。" actions={isAdmin ? <button className="cl-button cl-button-primary" onClick={() => setShowNew((value) => !value)}>{showNew ? '关闭新建' : '新建工作能力'} <span aria-hidden="true">＋</span></button> : undefined} />
    <div className="cl-intro"><div><span className="cl-kicker">怎么开始</span><strong>选一项工作能力 → 选项目 → 描述本次目标 → 开始工作</strong><p>能力会绑定项目的具体版本，运行开始后保留当时的工作说明。当前仍通过代码仓库执行，不代表已经配置云服务或飞书连接。</p></div><Link className="cl-link cl-intro-link" to="/projects">查看项目 →</Link></div>
    {error && <ErrorNotice message={error} />}{showNew && isAdmin && <CapabilityForm csrfToken={csrfToken} onUnauthorized={onUnauthorized} onSaved={saveCreated} onCancel={() => setShowNew(false)} />}
    <div className="cl-toolbar"><label className="cl-search"><span aria-hidden="true">⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工作名称、场景或分类" aria-label="搜索工作能力" /></label><span className="cl-total">{capabilities?.length ?? '—'} 项工作能力</span></div>
    <div className="cl-layout"><aside className="cl-library" aria-label="工作能力列表"><section className="cl-library-section"><div className="cl-section-heading"><div><h2>可直接使用</h2><p>已具备验收条件，可以从项目开始。</p></div><b>{ready.length}</b></div>{capabilities === null && !error && <div className="cl-list-loading"><span /><span /><span /></div>}{capabilities && ready.length === 0 && <EmptyMessage title="还没有可直接使用的工作" description="先从待完善模板开始，补充步骤和验收条件。" />}{ready.map((capability) => <CapabilityCard key={capability.id} capability={capability} selected={selected?.id === capability.id} onSelect={() => setSearchParams({ selected: capability.id }, { replace: true })} />)}</section><section className="cl-library-section"><div className="cl-section-heading"><div><h2>待完善</h2><p>模板和草稿只提供起点，不代表已验证。</p></div><b>{drafts.length}</b></div>{capabilities && drafts.length === 0 && <EmptyMessage title="没有待完善项目" description="创建一项工作能力，保存团队常做的工作方式。" />}{drafts.map((capability) => <CapabilityCard key={capability.id} capability={capability} selected={selected?.id === capability.id} onSelect={() => setSearchParams({ selected: capability.id }, { replace: true })} />)}</section></aside>
      <section className="cl-detail" id="cl-capability-detail">{selected ? <><header className="cl-detail-head"><div><div className="cl-detail-meta"><StatusPill status={selected.status} /><span>{categoryLabel[selected.category] ?? selected.category} · v{selected.revision}</span>{isTemplate(selected) && <span>内置模板</span>}</div><h2>{selected.name}</h2><p>{selected.description}</p></div><Link className="cl-link" to={`/capabilities?selected=${encodeURIComponent(selected.id)}`}>打开独立链接</Link></header><section className="cl-guide"><div><span className="cl-guide-label">什么时候用</span><p>{selected.description || '根据项目目标选择这项工作能力。'}</p></div><div><span className="cl-guide-label">需要提供什么</span><p>{selected.input_description || '项目、目标和边界。'}</p></div><div><span className="cl-guide-label">会交付什么</span><p>{selected.output_description || '可追溯的工作结果和检查记录。'}</p></div></section><InvokePanel key={`${selected.id}:${selected.revision}`} capability={selected} projects={authorizedProjects} projectsError={projectsError || memberProjectsError} projectsLoading={projectsLoading} isAdmin={isAdmin} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />{isAdmin && <details className="cl-management"><summary>管理与配置 <span>工作步骤、验收、版本、绑定和导出</span></summary><div className="cl-management-body"><CapabilityForm key={`form-${selected.id}:${selected.revision}`} capability={selected} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onSaved={(value) => { setSelected({ ...selected, ...value, versions: [...selected.versions, value] }); setCapabilities((items) => items?.map((item) => item.id === value.id ? value : item) ?? items) }} /><section className="cl-version-section"><div className="cl-subhead"><div><h3>版本记录</h3><p>项目绑定和运行使用具体版本，编辑会生成新版本。</p></div><span>{selected.versions.length} 个版本</span></div><div className="cl-version-list">{selected.versions.slice().reverse().map((version) => <div className="cl-version-row" key={version.revision}><strong>v{version.revision}</strong><span>{version.status === 'ready' ? '可直接使用' : '待完善'}</span><small>{formatDate(version.created_at)}</small></div>)}</div></section><section className="cl-export-section"><div><h2>交付 Agent 包</h2><p>导出当前固定版本和来源运行记录，便于留档或交接。</p></div><div className="cl-export-actions"><a className="cl-button cl-button-primary" href={`/api/v3/capabilities/${encodeURIComponent(selected.id)}/export?revision=${selected.revision}&format=zip`}>下载 ZIP</a><a className="cl-button cl-button-secondary" href={`/api/v3/capabilities/${encodeURIComponent(selected.id)}/export?revision=${selected.revision}&format=json`}>查看 JSON</a></div></section></div></details>}</> : <div className="cl-detail-empty"><span className="cl-empty-mark" aria-hidden="true">◌</span><h2>先选一项工作能力</h2><p>从左侧选择一项，先看它适合什么时候用，再决定是否从项目开始。</p></div>}</section>
    </div>
  </div>
}
