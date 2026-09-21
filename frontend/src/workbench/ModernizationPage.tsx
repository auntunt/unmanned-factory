import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText } from './ui'
import type { PageProps } from './ui'
import { ListTime } from './presentation'
import Icon from './Icon'
import type { V3Project } from './v3-types'
import type {
  ModernizationAgreementInfo,
  ModernizationDimension,
  ModernizationDimensionEntry,
  ModernizationDisposalEntry,
  ModernizationLocateResult,
  ModernizationSliceView,
  PluginAvailabilityView,
} from './modernization-types'
import {
  DIMENSIONS,
  currentStep,
  dimensionLabel,
  modernizationStatusLabel,
  modernizationStatusTone,
  pluginCreateBlockedReason,
  locateSourceLabel,
  stepLabel,
} from './modernization-types'

const API_PREFIX = '/api/v2/modernization'

interface ProjectBaseline {
  repository: string
  base_branch: string
}

function projectBaseline(project: V3Project): ProjectBaseline | null {
  if (!project.repository || !project.base_branch) return null
  return { repository: project.repository, base_branch: project.base_branch }
}

// --- Target dimensions ---

function TargetDimensionsCard({ csrfToken, onUnauthorized, projectId, createBlocked, onChanged }: {
  csrfToken: string
  onUnauthorized: () => void
  projectId: string
  createBlocked: string | null
  onChanged: () => void
}) {
  const [entries, setEntries] = useState<ModernizationDimensionEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Partial<Record<ModernizationDimension, string>>>({})
  const [source, setSource] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [confirmBusy, setConfirmBusy] = useState<string | null>(null)

  const load = () => {
    if (!projectId) return
    request<{ dimensions: ModernizationDimensionEntry[] }>(
      `${API_PREFIX}/targets?project_id=${encodeURIComponent(projectId)}`,
      { onUnauthorized },
    )
      .then(result => setEntries(result.dimensions))
      .catch(cause => {
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
  }

  useEffect(() => {
    setEntries(null)
    setError(null)
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  const submitTargets = async (event: FormEvent) => {
    event.preventDefault()
    const target: Record<string, string> = {}
    for (const dim of DIMENSIONS) {
      const value = (draft[dim] ?? '').trim()
      if (value) target[dim] = value
    }
    if (Object.keys(target).length === 0) return
    setBusy(true)
    setError(null)
    try {
      await request(`${API_PREFIX}/targets`, {
        method: 'POST', csrfToken, onUnauthorized,
        body: { project_id: projectId, target, source: source.trim() },
      })
      setDraft({})
      load()
      onChanged()
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  const confirm = async (dimension: ModernizationDimension) => {
    setConfirmBusy(dimension)
    setConfirmError(null)
    try {
      await request(`${API_PREFIX}/targets/confirm`, {
        method: 'POST', csrfToken, onUnauthorized,
        body: { project_id: projectId, dimension },
      })
      load()
      onChanged()
    } catch (cause) {
      setConfirmError(errorText(cause))
    } finally {
      setConfirmBusy(null)
    }
  }

  return (
    <section className="wb-card" aria-label="信创改造目标" data-testid="dimensions-card">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">目标登记</span>
          <h2>改造维度与目标</h2>
          <p>评估建议先以「候选」登记，人工确认后才是切片可依据的目标。</p>
        </div>
      </div>
      {error && <ErrorNotice message={error} />}
      {confirmError && <ErrorNotice message={confirmError} />}
      {!entries && !error && <p className="wb-runtime-note">正在读取目标…</p>}
      {entries && (
        <div className="wb-table-wrap">
          <table className="wb-table" data-testid="dimensions-table">
            <thead>
              <tr>
                <th>维度</th>
                <th>状态</th>
                <th>内容</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(entry => (
                <tr key={entry.dimension}>
                  <td>{entry.label}</td>
                  <td>
                    <span className={`wb-status ${entry.confirmed ? 'wb-status-success' : entry.status === 'missing' ? 'wb-status-neutral' : 'wb-status-warning'}`}>
                      <i aria-hidden="true" />
                      {entry.confirmed ? '已确认' : entry.status === 'missing' ? '未登记' : '候选'}
                    </span>
                  </td>
                  <td>{entry.content ?? '—'}</td>
                  <td>
                    {!entry.confirmed && entry.status !== 'missing' && !createBlocked && (
                      <button
                        className="wb-button wb-button-secondary"
                        disabled={confirmBusy === entry.dimension}
                        onClick={() => void confirm(entry.dimension)}
                      >
                        {confirmBusy === entry.dimension ? '确认中…' : '确认'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!createBlocked && (
        <form className="wb-form" onSubmit={submitTargets} style={{ padding: '0 20px 20px' }}>
          <div className="wb-form-grid wb-form-grid-two">
            {DIMENSIONS.map(dim => (
              <label key={dim}>
                {dimensionLabel(dim)}
                <input
                  value={draft[dim] ?? ''}
                  onChange={e => setDraft(current => ({ ...current, [dim]: e.target.value }))}
                  placeholder="留空则不更新"
                />
              </label>
            ))}
            <label className="wb-span-two">
              来源（可选）
              <input value={source} onChange={e => setSource(e.target.value)} placeholder="例如：迁移评估报告 v1" />
            </label>
          </div>
          <div className="wb-form-actions">
            <button className="wb-button wb-button-primary" disabled={busy}>
              {busy ? '登记中…' : '登记目标（候选）'}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}

// --- Disposal plan ---

function LocateResultView({ location }: { location: ModernizationLocateResult }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <strong>{location.query}</strong>
      {' '}
      <span className="wb-muted">
        {locateSourceLabel(location)}
      </span>
      {location.note && <p className="wb-runtime-note">{location.note}</p>}
      {location.results.length > 0 && (
        <ul className="wb-plain-list">
          {location.results.map((hit, i) => (
            <li key={i}>{hit.path}{hit.line ? `:${hit.line}` : ''} {hit.name ? `— ${hit.name}` : ''}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function DisposalPlanCard({ onUnauthorized, projectId }: {
  onUnauthorized: () => void
  projectId: string
}) {
  const [plan, setPlan] = useState<ModernizationDisposalEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = async () => {
    if (!projectId) return
    setBusy(true)
    setError(null)
    try {
      const result = await request<{ plan: ModernizationDisposalEntry[] }>(
        `${API_PREFIX}/disposal-plan?project_id=${encodeURIComponent(projectId)}`,
        { onUnauthorized },
      )
      setPlan(result.plan)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="wb-card" aria-label="依赖处置计划" data-testid="disposal-card">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">处置计划</span>
          <h2>依赖处置与代码定位</h2>
          <p>按维度给出目标状态与代码可能所在位置；未确认目标只作起草参考，不作为切片依据。</p>
        </div>
        <button className="wb-button wb-button-secondary" onClick={() => void load()} disabled={busy || !projectId}>
          {busy ? '查询中…' : '查看处置计划'}
        </button>
      </div>
      {error && <ErrorNotice message={error} />}
      {plan && (
        <div style={{ padding: '0 20px 20px' }}>
          {plan.map(entry => (
            <div key={entry.dimension} style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                {entry.label}
                {entry.blocked && (
                  <span className="wb-status wb-status-warning" style={{ marginLeft: 8 }}>
                    <i aria-hidden="true" />
                    {entry.blocked_reason}
                  </span>
                )}
              </h3>
              {entry.locations.map((loc, i) => <LocateResultView key={i} location={loc} />)}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// --- Create-slice form ---

interface SliceDraft {
  dimension: ModernizationDimension | ''
  scope_paths: string
  expected_behaviour: string
  delivery_goal: string
  delivery_tier: 'package' | 'authorized_test_project'
  known_gaps: string
}

const blankDraft: SliceDraft = {
  dimension: '',
  scope_paths: '',
  expected_behaviour: '',
  delivery_goal: '',
  delivery_tier: 'package',
  known_gaps: '',
}

function ModernizationCreateForm({ csrfToken, onUnauthorized, projects, projectId, onCreated, onCancel }: PageProps & {
  projects: V3Project[]
  projectId: string
  onCreated: (slice: ModernizationSliceView) => void
  onCancel: () => void
}) {
  const [draft, setDraft] = useState<SliceDraft>(blankDraft)
  const [baseSha, setBaseSha] = useState('')
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [agreement, setAgreement] = useState<ModernizationAgreementInfo | null>(null)
  const [agreementError, setAgreementError] = useState<string | null>(null)
  const [locateQuery, setLocateQuery] = useState('')
  const [locateResult, setLocateResult] = useState<ModernizationLocateResult | null>(null)
  const [locateBusy, setLocateBusy] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  useEffect(() => {
    const controller = new AbortController()
    request<ModernizationAgreementInfo>(`${API_PREFIX}/agreement`, {
      onUnauthorized, signal: controller.signal,
    })
      .then(info => { if (!controller.signal.aborted) setAgreement(info) })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setAgreementError(errorText(cause))
      })
    return () => controller.abort()
  }, [onUnauthorized])

  const selectedProject = projects.find(p => String(p.id) === projectId)
  const baseline = selectedProject ? projectBaseline(selectedProject) : null

  const update = <K extends keyof SliceDraft>(key: K, value: SliceDraft[K]) =>
    setDraft(current => ({ ...current, [key]: value }))

  const runLocate = async () => {
    if (!locateQuery.trim() || !projectId) return
    setLocateBusy(true)
    try {
      const result = await request<ModernizationLocateResult>(
        `${API_PREFIX}/locate?project_id=${encodeURIComponent(projectId)}&q=${encodeURIComponent(locateQuery.trim())}&limit=8`,
        { onUnauthorized },
      )
      setLocateResult(result)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setLocateBusy(false)
    }
  }

  const addToScope = (path: string) => {
    const existing = draft.scope_paths.split('\n').map(s => s.trim()).filter(Boolean)
    if (!existing.includes(path)) update('scope_paths', [...existing, path].join('\n'))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (busy) return
    if (!projectId) { setError('请选择项目。'); return }
    if (!draft.dimension) { setError('请选择改造维度。'); return }
    const scopePaths = draft.scope_paths.split('\n').map(s => s.trim()).filter(Boolean)
    if (scopePaths.length === 0) { setError('请给出至少一个具体范围路径，不能是整仓库。'); return }
    if (!baseSha.trim() || baseSha.trim().length !== 40) { setError('请填写完整的 40 位基线 SHA。'); return }
    if (!draft.expected_behaviour.trim()) { setError('请填写期望行为。'); return }
    if (!draft.delivery_goal.trim()) { setError('请填写交付目标。'); return }
    if (!agreement) { setError('还没有拿到适用的方法版本，暂时不能提交。'); return }

    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    try {
      const knownGaps = draft.known_gaps.split('\n').map(s => s.trim()).filter(Boolean)
      const body = {
        idempotency_key: idempotencyKey,
        project_id: projectId,
        repository: baseline!.repository,
        base_sha: baseSha.trim(),
        base_branch_label: baseline!.base_branch,
        dimension: draft.dimension,
        scope_paths: scopePaths,
        expected_behaviour: draft.expected_behaviour.trim(),
        delivery_goal: draft.delivery_goal.trim(),
        delivery_tier: draft.delivery_tier,
        known_gaps: knownGaps,
        agreement: { revision: agreement.revision, skill_version: agreement.skill_version },
      }
      const slice = await request<ModernizationSliceView>(`${API_PREFIX}/slices`, {
        method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body,
      })
      if (!controller.signal.aborted) {
        onCreated(slice)
        setDraft(blankDraft)
        setBaseSha('')
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause))
    } finally {
      if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false)
    }
  }

  return (
    <section className="wb-card wb-create-card" aria-labelledby="create-modernization-title">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">新建改造切片</span>
          <h2 id="create-modernization-title">新建改造切片</h2>
          <p>对一条范围明确的切片实际发起编码执行，不是只出一份计划。</p>
          {agreement && (
            <p className="wb-muted" data-testid="agreement-line">
              本次适用方法：{agreement.pack.name}（{agreement.skill_version}，
              {agreement.pack.validation_status}）
            </p>
          )}
          {agreementError && <ErrorNotice message={`方法版本读取失败：${agreementError}`} />}
        </div>
        <button className="wb-icon-button wb-close-button" type="button" onClick={onCancel} aria-label="关闭">
          ×
        </button>
      </div>
      <form className="wb-form" onSubmit={submit}>
        <label>
          改造维度
          <select
            required
            value={draft.dimension}
            onChange={e => update('dimension', e.target.value as ModernizationDimension)}
          >
            <option value="">请选择维度</option>
            {DIMENSIONS.map(dim => (
              <option key={dim} value={dim}>{dimensionLabel(dim)}</option>
            ))}
          </select>
        </label>
        {baseline && (
          <div className="wb-form-grid wb-form-grid-two">
            <label>
              仓库（只读）
              <input value={baseline.repository} readOnly disabled />
            </label>
            <label>
              分支标签（只读）
              <input value={baseline.base_branch} readOnly disabled />
            </label>
            <label className="wb-span-two">
              基线 SHA（完整 40 位）
              <input
                required
                maxLength={40}
                minLength={40}
                pattern="[0-9a-f]{40}"
                value={baseSha}
                onChange={e => setBaseSha(e.target.value.toLowerCase().replace(/[^0-9a-f]/g, ''))}
                placeholder="完整 40 位 commit SHA"
              />
              <small>请填写仓库 {baseline.base_branch} 分支上用作本次改造基线的完整 SHA。</small>
            </label>
          </div>
        )}
        <div>
          <label>
            代码定位（可选）：搜索并加入范围
            <div className="wb-form-grid wb-form-grid-two">
              <input value={locateQuery} onChange={e => setLocateQuery(e.target.value)} placeholder="例如：datasource、jdbc" />
              <button type="button" className="wb-button wb-button-secondary" onClick={() => void runLocate()} disabled={locateBusy || !projectId}>
                {locateBusy ? '搜索中…' : '搜索定位'}
              </button>
            </div>
          </label>
          {locateResult && (
            <div data-testid="locate-result">
              {locateResult.note && <p className="wb-runtime-note">{locateResult.note}</p>}
              <ul className="wb-plain-list">
                {locateResult.results.map((hit, i) => (
                  <li key={i}>
                    {hit.path}{hit.line ? `:${hit.line}` : ''}
                    <button type="button" className="wb-button wb-button-secondary" style={{ marginLeft: 8 }} onClick={() => addToScope(hit.path)}>
                      加入范围
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <label>
          范围路径（每行一个，不能是整仓库）
          <textarea
            required
            rows={3}
            value={draft.scope_paths}
            onChange={e => update('scope_paths', e.target.value)}
            placeholder={'例如：\nsrc/main/java/com/example/DataSourceConfig.java'}
          />
        </label>
        <label>
          期望行为
          <textarea
            required
            rows={3}
            value={draft.expected_behaviour}
            onChange={e => update('expected_behaviour', e.target.value)}
            placeholder="描述改造后系统应呈现的行为"
          />
        </label>
        <label>
          交付目标
          <textarea
            required
            rows={2}
            value={draft.delivery_goal}
            onChange={e => update('delivery_goal', e.target.value)}
            placeholder="例如：补丁通过全部检查，合入主分支"
          />
        </label>
        <div className="wb-form-grid wb-form-grid-two">
          <label>
            交付层级
            <select value={draft.delivery_tier} onChange={e => update('delivery_tier', e.target.value as SliceDraft['delivery_tier'])}>
              <option value="package">打包交付</option>
              <option value="authorized_test_project">授权测试项目</option>
            </select>
          </label>
          <label>
            已知未验证条件（每行一个，可选）
            <textarea
              rows={2}
              value={draft.known_gaps}
              onChange={e => update('known_gaps', e.target.value)}
              placeholder={'例如：\n无达梦实例，数据库验证无法在本轮完成'}
            />
          </label>
        </div>
        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>
            取消
          </button>
          <button className="wb-button wb-button-primary" disabled={busy}>
            {busy ? '提交中…' : '创建切片'}
          </button>
        </div>
      </form>
    </section>
  )
}

// --- Slice list ---

function ModernizationStatusBadge({ status }: { status: string }) {
  const tone = modernizationStatusTone(status as import('./modernization-types').ModernizationStatus)
  return (
    <span className={`wb-status wb-status-${tone}`}>
      <i aria-hidden="true" />
      {modernizationStatusLabel(status as import('./modernization-types').ModernizationStatus)}
    </span>
  )
}

export default function ModernizationPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const isAdmin = user?.role !== 'member'
  const [slices, setSlices] = useState<ModernizationSliceView[] | null>(null)
  const [projects, setProjects] = useState<V3Project[]>([])
  const [error, setError] = useState<string | null>(null)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(() => searchParams.get('create') === '1')
  const [refreshIndex, setRefreshIndex] = useState(0)
  const [availability, setAvailability] = useState<PluginAvailabilityView | null>(null)
  const requested = searchParams.get('project_id') ?? ''

  const projectFilter = requested || (projects.length > 0 ? String(projects[0].id) : '')

  useEffect(() => {
    const controller = new AbortController()
    setProjectsError(null)
    request<{ projects: V3Project[] }>('/api/v2/projects', {
      onUnauthorized,
      signal: controller.signal,
    })
      .then(result => { if (!controller.signal.aborted) setProjects(result.projects) })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setProjectsError(`项目列表加载失败：${errorText(cause)}`)
      })
    return () => controller.abort()
  }, [onUnauthorized, refreshIndex])

  useEffect(() => {
    const controller = new AbortController()
    request<PluginAvailabilityView>(`${API_PREFIX}/availability`, {
      onUnauthorized, signal: controller.signal,
    })
      .then(result => { if (!controller.signal.aborted) setAvailability(result) })
      .catch(() => { /* 读不到可用性就保持当前入口，真正的拒绝在服务端 */ })
    return () => controller.abort()
  }, [onUnauthorized, refreshIndex])

  useEffect(() => {
    if (!projectFilter) return
    const controller = new AbortController()
    setError(null)
    request<{ slices: ModernizationSliceView[]; availability?: PluginAvailabilityView }>(
      `${API_PREFIX}/slices?project_id=${encodeURIComponent(projectFilter)}`,
      { onUnauthorized, signal: controller.signal },
    )
      .then(result => {
        if (controller.signal.aborted) return
        setSlices(result.slices)
        if (result.availability) setAvailability(result.availability)
      })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
    const timer = window.setInterval(() => setRefreshIndex(v => v + 1), 8000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [onUnauthorized, refreshIndex, projectFilter])

  const projectNames = new Map(projects.map(p => [String(p.id), p.name]))
  const visible = (slices ?? [])
    .filter(s => !projectFilter || s.project_id === projectFilter)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))

  const projectPicker = (
    <label className="wb-inline-field">
      项目
      <select
        value={projectFilter}
        aria-label="选择项目"
        onChange={e => {
          const next = new URLSearchParams(searchParams)
          next.set('project_id', e.target.value)
          setSearchParams(next, { replace: true })
          setSlices(null)
        }}
      >
        {projects.length === 0 && <option value="">（没有可见的项目）</option>}
        {projects.map(p => (
          <option key={String(p.id)} value={String(p.id)}>{p.name}</option>
        ))}
      </select>
    </label>
  )

  const createBlocked = pluginCreateBlockedReason(availability)

  return (
    <div className="wb-page">
      <PageHeader
        title="信创化改造"
        description="登记改造目标、查看依赖处置与代码定位，发起并跟踪一条改造切片。"
        actions={
          <>
            {projectPicker}
            <button className="wb-button wb-button-secondary" onClick={() => setRefreshIndex(v => v + 1)}>
              刷新
            </button>
            {isAdmin && !createBlocked && (
              <button className="wb-button wb-button-primary" onClick={() => setCreateOpen(v => !v)}>
                {createOpen ? '关闭新建' : '新建切片'}{' '}
                <span aria-hidden="true">
                  <Icon name="plus" />
                </span>
              </button>
            )}
          </>
        }
      />

      {createBlocked && (
        <div className="wb-notice" role="status">
          {createBlocked}
        </div>
      )}

      {error && <ErrorNotice message={error} />}
      {projectsError && <ErrorNotice message={projectsError} />}

      {!projectFilter && !projectsError && (
        <div className="wb-card">
          <EmptyState
            title="还没有可查看的项目"
            description="信创化改造按项目授权查看，请先在「项目」里登记一个项目。"
          />
        </div>
      )}

      {projectFilter && (
        <>
          <TargetDimensionsCard
            csrfToken={csrfToken}
            onUnauthorized={onUnauthorized}
            projectId={projectFilter}
            createBlocked={createBlocked}
            onChanged={() => setRefreshIndex(v => v + 1)}
          />
          <DisposalPlanCard onUnauthorized={onUnauthorized} projectId={projectFilter} />
        </>
      )}

      {createOpen && isAdmin && !createBlocked && projectFilter && (
        <ModernizationCreateForm
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          user={user}
          projects={projects}
          projectId={projectFilter}
          onCreated={slice => {
            setSlices(current => (current ? [slice, ...current] : [slice]))
            setCreateOpen(false)
          }}
          onCancel={() => setCreateOpen(false)}
        />
      )}

      {projectFilter && !slices && !error && (
        <div className="wb-card">
          <div className="wb-list-placeholder">
            <span />
            <span />
            <span />
          </div>
        </div>
      )}

      {slices && visible.length === 0 && (
        <div className="wb-card">
          <EmptyState
            title="还没有改造切片"
            description={
              isAdmin
                ? '这个项目还没有改造切片；创建后系统会自动领取和执行。'
                : '管理员为这个项目创建改造切片后，会出现在这里。'
            }
          />
        </div>
      )}

      {slices && visible.length > 0 && (
        <section className="wb-card wb-run-table-card">
          <div className="wb-table-wrap">
            <table className="wb-table wb-runs-table">
              <thead>
                <tr>
                  <th>切片</th>
                  <th>维度</th>
                  <th>项目</th>
                  <th>状态</th>
                  <th>阶段</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.map(slice => (
                  <tr key={slice.slice_id}>
                    <td>
                      <Link className="wb-table-link" to={`/modernization/${encodeURIComponent(slice.slice_id)}`}>
                        {slice.slice_id.slice(0, 8)}
                      </Link>
                    </td>
                    <td>{dimensionLabel(slice.dimension)}</td>
                    <td>{projectNames.get(slice.project_id) ?? slice.project_id}</td>
                    <td>
                      <ModernizationStatusBadge status={slice.status} />
                    </td>
                    <td>
                      {currentStep(slice.steps) ? stepLabel(currentStep(slice.steps)!.step) : '—'}
                    </td>
                    <td>
                      <ListTime value={slice.created_at} />
                    </td>
                    <td>
                      <Link className="wb-button wb-button-secondary" to={`/modernization/${encodeURIComponent(slice.slice_id)}`}>
                        查看详情
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
