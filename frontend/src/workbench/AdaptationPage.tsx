import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText } from './ui'
import type { PageProps } from './ui'
import { ListTime } from './presentation'
import Icon from './Icon'
import type {
  AdaptationTaskView,
  PluginAvailabilityView,
} from './adaptation-types'
import {
  adaptationStatusLabel,
  adaptationStatusTone,
  businessCompletedLabel,
  mockLabel,
  mockTone,
  pluginCreateBlockedReason,
} from './adaptation-types'
import type { V3Project } from './v3-types'

const API_PREFIX = '/api/v2/adaptation'

// --- Create / revise form ---
// One shared form for both actions: a revise is "the same adaptation
// implementation, a new contract version" (api_adaptation.revise), not a
// different workflow — so it reuses the same fields, locks the project, and
// posts to /tasks/{id}/revise instead of /tasks.

interface ContractDraft {
  source_api_name: string
  source_api_version: string
  source_api_base_url: string
  source_api_auth: string
  source_api_environment: 'mock' | 'sandbox' | 'production'
  target_adapter_name: string
  target_adapter_entry: string
  fields_json: string
  endpoints_json: string
}

interface MessageDraft {
  name: string
  method: string
  path: string
  idempotent: boolean
  retry_policy: string
  sample_payload_json: string
}

interface TaskDraft {
  project_id: string
  base_sha: string
  contract: ContractDraft
  mapping_matrix_json: string
  message: MessageDraft
  expected_behaviour: string
  delivery_goal: string
  delivery_tier: 'package' | 'authorized_test_project'
  synthetic: boolean
  agreement_revision: string
  agreement_skill_version: string
}

const MAPPING_PLACEHOLDER = JSON.stringify(
  [
    { source_field: 'order_id', mapped: true, target_field: 'orderId', type: 'string' },
    { source_field: 'legacy_flag', mapped: false, impact: '目标系统没有对应语义，业务上按默认值处理，需人工确认' },
  ],
  null,
  2,
)

function blankDraft(project_id = ''): TaskDraft {
  return {
    project_id,
    base_sha: '',
    contract: {
      source_api_name: '', source_api_version: '', source_api_base_url: '',
      source_api_auth: '', source_api_environment: 'mock',
      target_adapter_name: '', target_adapter_entry: '',
      fields_json: '{}', endpoints_json: '[]',
    },
    mapping_matrix_json: '',
    message: { name: '', method: 'POST', path: '', idempotent: false, retry_policy: '', sample_payload_json: '{}' },
    expected_behaviour: '',
    delivery_goal: '',
    delivery_tier: 'package',
    synthetic: false,
    agreement_revision: '',
    agreement_skill_version: '',
  }
}

function draftFromTask(task: AdaptationTaskView): TaskDraft {
  return {
    project_id: task.project_id,
    base_sha: task.baseline.base_sha,
    contract: {
      source_api_name: task.contract.source_api.name,
      source_api_version: task.contract.source_api.version,
      source_api_base_url: task.contract.source_api.base_url,
      source_api_auth: task.contract.source_api.auth,
      source_api_environment: task.contract.source_api.environment,
      target_adapter_name: task.contract.target_adapter.name,
      target_adapter_entry: task.contract.target_adapter.entry,
      fields_json: JSON.stringify(task.contract.fields ?? {}, null, 2),
      endpoints_json: JSON.stringify(task.contract.endpoints ?? [], null, 2),
    },
    mapping_matrix_json: JSON.stringify(task.mapping_matrix, null, 2),
    message: {
      name: task.message.name, method: task.message.method, path: task.message.path,
      idempotent: task.message.idempotent, retry_policy: task.message.retry_policy,
      sample_payload_json: JSON.stringify(task.message.sample_payload ?? {}, null, 2),
    },
    expected_behaviour: task.expected_behaviour,
    delivery_goal: task.delivery_goal,
    delivery_tier: (task.delivery_tier as 'package' | 'authorized_test_project') || 'package',
    synthetic: task.synthetic,
    agreement_revision: task.agreement.revision,
    agreement_skill_version: task.agreement.skill_version,
  }
}

function projectRepo(project?: V3Project): { repository: string; base_branch: string } | null {
  if (!project?.repository || !project.base_branch) return null
  return { repository: project.repository, base_branch: project.base_branch }
}

function AdaptationTaskForm({ csrfToken, onUnauthorized, projects, reviseTarget, onSubmitted, onCancel }: PageProps & {
  projects: V3Project[]
  reviseTarget: AdaptationTaskView | null
  onSubmitted: (task: AdaptationTaskView) => void
  onCancel: () => void
}) {
  const mode = reviseTarget ? 'revise' : 'create'
  const [draft, setDraft] = useState<TaskDraft>(() => reviseTarget ? draftFromTask(reviseTarget) : blankDraft())
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  const selectedProject = projects.find(p => String(p.id) === draft.project_id)
  const baseline = selectedProject ? projectRepo(selectedProject) : null

  const update = <K extends keyof TaskDraft>(key: K, value: TaskDraft[K]) =>
    setDraft(current => ({ ...current, [key]: value }))
  const updateContract = <K extends keyof ContractDraft>(key: K, value: ContractDraft[K]) =>
    setDraft(current => ({ ...current, contract: { ...current.contract, [key]: value } }))
  const updateMessage = <K extends keyof MessageDraft>(key: K, value: MessageDraft[K]) =>
    setDraft(current => ({ ...current, message: { ...current.message, [key]: value } }))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (busy) return
    if (!draft.project_id) { setError('请选择项目。'); return }
    if (!baseline) { setError('所选项目还没有登记仓库/分支信息。'); return }
    if (!draft.base_sha.trim() || draft.base_sha.trim().length !== 40) { setError('请填写完整的 40 位基线 SHA。'); return }
    const c = draft.contract
    if (!c.source_api_name.trim() || !c.source_api_version.trim() || !c.source_api_base_url.trim() || !c.source_api_auth.trim()) {
      setError('请完整填写来源接口（名称、版本、base_url、鉴权方式）。'); return
    }
    if (!c.target_adapter_name.trim() || !c.target_adapter_entry.trim()) {
      setError('请完整填写目标 Adapter（名称、入口）。'); return
    }
    let fields: Record<string, unknown>
    let endpoints: unknown[]
    try { fields = JSON.parse(c.fields_json || '{}') } catch { setError('字段规格 JSON 格式不对。'); return }
    try { endpoints = JSON.parse(c.endpoints_json || '[]') } catch { setError('HTTP 操作（endpoints）JSON 格式不对。'); return }
    let mappingMatrix: unknown
    try { mappingMatrix = JSON.parse(draft.mapping_matrix_json) } catch { setError('字段映射矩阵必须是合法 JSON。'); return }
    if (!Array.isArray(mappingMatrix) || mappingMatrix.length === 0) {
      setError('字段映射矩阵不能为空，必须覆盖所有源字段。'); return
    }
    for (const raw of mappingMatrix) {
      const entry = raw as Record<string, unknown>
      if (!entry || typeof entry !== 'object' || !entry.source_field) {
        setError('字段映射矩阵每一项都必须有 source_field。'); return
      }
      const mapped = Boolean(entry.mapped ?? entry.target_field)
      if (mapped && !entry.target_field) {
        setError(`字段 ${String(entry.source_field)} 标记为已映射，但缺少 target_field。`); return
      }
      if (!mapped && !entry.impact) {
        setError(`字段 ${String(entry.source_field)} 无法映射，必须说明 impact（对业务的影响），不能静默丢弃。`); return
      }
    }
    let samplePayload: Record<string, unknown>
    try { samplePayload = JSON.parse(draft.message.sample_payload_json || '{}') } catch { setError('业务报文示例（sample_payload）JSON 格式不对。'); return }
    if (!draft.message.name.trim() || !draft.message.method.trim() || !draft.message.path.trim()) {
      setError('请完整填写业务报文（名称、方法、路径）。'); return
    }
    if (!draft.message.idempotent && !draft.message.retry_policy.trim()) {
      setError('非幂等业务报文必须显式说明重试/去重策略，超时不许盲重试。'); return
    }
    if (!draft.expected_behaviour.trim()) { setError('请填写期望行为。'); return }
    if (!draft.delivery_goal.trim()) { setError('请填写交付目标。'); return }
    if (!draft.agreement_revision.trim()) { setError('请填写适用的约定版本。'); return }

    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    try {
      const body = {
        idempotency_key: idempotencyKey,
        project_id: draft.project_id,
        repository: baseline.repository,
        base_sha: draft.base_sha.trim(),
        base_branch_label: baseline.base_branch,
        contract: {
          source_api: {
            name: c.source_api_name.trim(), version: c.source_api_version.trim(),
            base_url: c.source_api_base_url.trim(), auth: c.source_api_auth.trim(),
            environment: c.source_api_environment,
          },
          target_adapter: { name: c.target_adapter_name.trim(), entry: c.target_adapter_entry.trim() },
          endpoints, fields,
        },
        mapping_matrix: mappingMatrix,
        message: {
          name: draft.message.name.trim(), method: draft.message.method.trim().toUpperCase(),
          path: draft.message.path.trim(), idempotent: draft.message.idempotent,
          retry_policy: draft.message.retry_policy.trim(), sample_payload: samplePayload,
        },
        expected_behaviour: draft.expected_behaviour.trim(),
        delivery_goal: draft.delivery_goal.trim(),
        delivery_tier: draft.delivery_tier,
        synthetic: draft.synthetic,
        agreement: { revision: draft.agreement_revision.trim(), skill_version: draft.agreement_skill_version.trim() },
      }
      const path = mode === 'revise' && reviseTarget
        ? `${API_PREFIX}/tasks/${encodeURIComponent(reviseTarget.task_id)}/revise`
        : `${API_PREFIX}/tasks`
      const task = await request<AdaptationTaskView>(path, {
        method: 'POST', csrfToken, onUnauthorized, signal: controller.signal, body,
      })
      if (!controller.signal.aborted) onSubmitted(task)
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause))
    } finally {
      if (controllerRef.current === controller && !controller.signal.aborted) setBusy(false)
    }
  }

  return (
    <section className="wb-card wb-create-card" aria-labelledby="create-adaptation-title">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">{mode === 'revise' ? '导入下一版契约' : '新建适配任务'}</span>
          <h2 id="create-adaptation-title">{mode === 'revise' ? `修订任务 ${reviseTarget!.task_id.slice(0, 8)}` : '新建接口适配任务'}</h2>
          <p>
            {mode === 'revise'
              ? '导入新的契约版本：系统会对比字段差异、定位受影响映射，并作为同一适配实现的新修订处理，旧版本的映射与回执保留不变。'
              : '登记来源接口契约、字段映射矩阵与一条真实业务报文，提交后系统将自动领取和执行。'}
          </p>
        </div>
        <button className="wb-icon-button wb-close-button" type="button" onClick={onCancel} aria-label="关闭">×</button>
      </div>
      <form className="wb-form" onSubmit={submit}>
        <label>
          项目
          <select
            required
            disabled={mode === 'revise'}
            value={draft.project_id}
            onChange={e => update('project_id', e.target.value)}
          >
            <option value="">请选择已注册的项目</option>
            {projects.map(p => <option key={String(p.id)} value={String(p.id)}>{p.name}</option>)}
          </select>
        </label>
        {baseline && (
          <div className="wb-form-grid wb-form-grid-two">
            <label>仓库（只读）<input value={baseline.repository} readOnly disabled /></label>
            <label>分支标签（只读）<input value={baseline.base_branch} readOnly disabled /></label>
            <label className="wb-span-two">
              基线 SHA（完整 40 位）
              <input
                required maxLength={40} minLength={40} pattern="[0-9a-f]{40}"
                value={draft.base_sha}
                onChange={e => update('base_sha', e.target.value.toLowerCase().replace(/[^0-9a-f]/g, ''))}
                placeholder="完整 40 位 commit SHA"
              />
            </label>
          </div>
        )}

        <fieldset className="wb-form-grid wb-form-grid-two">
          <legend>来源接口（source_api）</legend>
          <label>名称<input required value={draft.contract.source_api_name} onChange={e => updateContract('source_api_name', e.target.value)} /></label>
          <label>版本<input required value={draft.contract.source_api_version} onChange={e => updateContract('source_api_version', e.target.value)} /></label>
          <label className="wb-span-two">base_url<input required value={draft.contract.source_api_base_url} onChange={e => updateContract('source_api_base_url', e.target.value)} placeholder="可指向本地模拟第三方端点" /></label>
          <label>鉴权方式<input required value={draft.contract.source_api_auth} onChange={e => updateContract('source_api_auth', e.target.value)} placeholder="凭据本身从配置读取，这里只写方式" /></label>
          <label>
            环境
            <select value={draft.contract.source_api_environment} onChange={e => updateContract('source_api_environment', e.target.value as ContractDraft['source_api_environment'])}>
              <option value="mock">mock（本地模拟）</option>
              <option value="sandbox">sandbox（厂商沙箱）</option>
              <option value="production">production（真实第三方）</option>
            </select>
          </label>
        </fieldset>

        <fieldset className="wb-form-grid wb-form-grid-two">
          <legend>目标 Adapter（target_adapter）</legend>
          <label>名称<input required value={draft.contract.target_adapter_name} onChange={e => updateContract('target_adapter_name', e.target.value)} /></label>
          <label>入口（entry）<input required value={draft.contract.target_adapter_entry} onChange={e => updateContract('target_adapter_entry', e.target.value)} placeholder="例如 adapters/foo.py:handle" /></label>
        </fieldset>

        <label>
          字段规格 JSON（contract.fields，字段名到规格的映射，可留空对象）
          <textarea rows={4} value={draft.contract.fields_json} onChange={e => updateContract('fields_json', e.target.value)} />
        </label>
        <label>
          HTTP 操作 JSON（contract.endpoints，仅用于统计展示，可留空数组）
          <textarea rows={3} value={draft.contract.endpoints_json} onChange={e => updateContract('endpoints_json', e.target.value)} />
        </label>

        <label>
          字段映射矩阵 JSON（每项必须有 source_field，已映射需给 target_field，无法映射需给 impact）
          <textarea
            required rows={10}
            value={draft.mapping_matrix_json}
            onChange={e => update('mapping_matrix_json', e.target.value)}
            placeholder={MAPPING_PLACEHOLDER}
          />
          <small>待确认/无法映射的字段不能静默丢弃，必须写明 impact（对业务的影响）。</small>
        </label>

        <fieldset className="wb-form-grid wb-form-grid-two">
          <legend>本次处理的业务报文（message）</legend>
          <label>名称<input required value={draft.message.name} onChange={e => updateMessage('name', e.target.value)} /></label>
          <label>
            方法
            <select required value={draft.message.method} onChange={e => updateMessage('method', e.target.value)}>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <label className="wb-span-two">路径<input required value={draft.message.path} onChange={e => updateMessage('path', e.target.value)} /></label>
          <label>
            <input type="checkbox" checked={draft.message.idempotent} onChange={e => updateMessage('idempotent', e.target.checked)} />{' '}
            该报文是幂等操作
          </label>
          {!draft.message.idempotent && (
            <label className="wb-span-two">
              重试/去重策略（非幂等操作必填）
              <input required value={draft.message.retry_policy} onChange={e => updateMessage('retry_policy', e.target.value)} placeholder="超时不得盲重试，先核对宿主/网关是否已有重试与去重能力" />
            </label>
          )}
          <label className="wb-span-two">
            业务报文示例 JSON（sample_payload，可留空对象）
            <textarea rows={3} value={draft.message.sample_payload_json} onChange={e => updateMessage('sample_payload_json', e.target.value)} />
          </label>
        </fieldset>

        <label>期望行为<textarea required rows={3} value={draft.expected_behaviour} onChange={e => update('expected_behaviour', e.target.value)} /></label>
        <label>交付目标<textarea required rows={2} value={draft.delivery_goal} onChange={e => update('delivery_goal', e.target.value)} placeholder="例如：适配代码通过检查，真实调用一次业务报文并正确解释结果" /></label>

        <div className="wb-form-grid wb-form-grid-two">
          <label>
            交付层级
            <select value={draft.delivery_tier} onChange={e => update('delivery_tier', e.target.value as TaskDraft['delivery_tier'])}>
              <option value="package">package（交包待发布，不部署）</option>
              <option value="authorized_test_project">authorized_test_project（已授权测试项目）</option>
            </select>
          </label>
          <label>
            <input type="checkbox" checked={draft.synthetic} onChange={e => update('synthetic', e.target.checked)} />{' '}
            这是合成示例契约（非真实客户验收）
          </label>
        </div>

        <div className="wb-form-grid wb-form-grid-two">
          <label>适用约定版本（revision）<input required value={draft.agreement_revision} onChange={e => update('agreement_revision', e.target.value)} /></label>
          <label>Skill 版本（可选）<input value={draft.agreement_skill_version} onChange={e => update('agreement_skill_version', e.target.value)} /></label>
        </div>
        <small className="wb-muted">本模块暂无独立的方法版本查询接口，约定版本需要手工填写并与实际使用的 Skill 一致。</small>

        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button>
          <button className="wb-button wb-button-primary" disabled={busy}>
            {busy ? '提交中…' : mode === 'revise' ? '提交新修订' : '创建任务'}
          </button>
        </div>
      </form>
    </section>
  )
}

// --- Task list ---

function AdaptationStatusBadge({ status }: { status: AdaptationTaskView['status'] }) {
  const tone = adaptationStatusTone(status)
  return <span className={`wb-status wb-status-${tone}`}><i aria-hidden="true" />{adaptationStatusLabel(status)}</span>
}

function BusinessCallSummary({ task }: { task: AdaptationTaskView }) {
  if (!task.business_call) return <span className="wb-muted">未联调</span>
  const tone = mockTone(task.business_call)
  return (
    <span className={`wb-status wb-status-${tone}`} title={mockLabel(task.business_call)}>
      <i aria-hidden="true" />
      {task.business_call.mock ? 'mock' : '真实'} / {businessCompletedLabel(task.business_call)}
    </span>
  )
}

export default function AdaptationPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const isAdmin = user?.role !== 'member'
  const [tasks, setTasks] = useState<AdaptationTaskView[] | null>(null)
  const [projects, setProjects] = useState<V3Project[]>([])
  const [error, setError] = useState<string | null>(null)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(() => searchParams.get('create') === '1')
  const [refreshIndex, setRefreshIndex] = useState(0)
  const [availability, setAvailability] = useState<PluginAvailabilityView | null>(null)
  const requested = searchParams.get('project_id') ?? ''
  const reviseId = searchParams.get('revise')

  const projectFilter = requested || (projects.length > 0 ? String(projects[0].id) : '')

  useEffect(() => {
    const controller = new AbortController()
    setProjectsError(null)
    request<{ projects: V3Project[] }>('/api/v2/projects', { onUnauthorized, signal: controller.signal })
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
    request<PluginAvailabilityView>(`${API_PREFIX}/availability`, { onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setAvailability(result) })
      .catch(() => { /* 读不到可用性就保持当前入口，真正的拒绝在服务端 */ })
    return () => controller.abort()
  }, [onUnauthorized, refreshIndex])

  useEffect(() => {
    if (!projectFilter) return
    const controller = new AbortController()
    setError(null)
    request<{ tasks: AdaptationTaskView[]; availability?: PluginAvailabilityView }>(
      `${API_PREFIX}/tasks?project_id=${encodeURIComponent(projectFilter)}`,
      { onUnauthorized, signal: controller.signal },
    )
      .then(result => {
        if (controller.signal.aborted) return
        setTasks(result.tasks)
        if (result.availability) setAvailability(result.availability)
      })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
    const timer = window.setInterval(() => setRefreshIndex(v => v + 1), 8000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [onUnauthorized, refreshIndex, projectFilter])

  const projectNames = new Map(projects.map(p => [String(p.id), p.name]))
  const visible = (tasks ?? [])
    .filter(t => !projectFilter || t.project_id === projectFilter)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))

  const reviseTarget = reviseId ? visible.find(t => t.task_id === reviseId) ?? (tasks ?? []).find(t => t.task_id === reviseId) ?? null : null

  const closeForms = () => {
    setCreateOpen(false)
    const next = new URLSearchParams(searchParams)
    next.delete('revise'); next.delete('create')
    setSearchParams(next, { replace: true })
  }

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
          setTasks(null)
        }}
      >
        {projects.length === 0 && <option value="">（没有可见的项目）</option>}
        {projects.map(p => <option key={String(p.id)} value={String(p.id)}>{p.name}</option>)}
      </select>
    </label>
  )

  const createBlocked = pluginCreateBlockedReason(availability)
  const formOpen = createOpen || Boolean(reviseId)

  return (
    <div className="wb-page">
      <PageHeader
        title="接口适配任务"
        description="登记三方接口契约、字段映射与业务报文，追踪执行进度、取得交付与联调结果。"
        actions={
          <>
            {projectPicker}
            <button className="wb-button wb-button-secondary" onClick={() => setRefreshIndex(v => v + 1)}>刷新</button>
            {isAdmin && !createBlocked && (
              <button className="wb-button wb-button-primary" onClick={() => { setCreateOpen(v => !v); if (reviseId) closeForms() }}>
                {createOpen ? '关闭新建' : '新建任务'} <span aria-hidden="true"><Icon name="plus" /></span>
              </button>
            )}
          </>
        }
      />

      {createBlocked && <div className="wb-notice" role="status">{createBlocked}</div>}

      {formOpen && isAdmin && !createBlocked && (
        <AdaptationTaskForm
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          user={user}
          projects={projects}
          reviseTarget={reviseTarget}
          onSubmitted={task => {
            setTasks(current => {
              if (!current) return [task]
              const withoutPredecessor = current.map(t => t.task_id === task.predecessor_id ? { ...t, successor_id: task.task_id } : t)
              return [task, ...withoutPredecessor]
            })
            closeForms()
          }}
          onCancel={closeForms}
        />
      )}

      {error && <ErrorNotice message={error} />}
      {projectsError && <ErrorNotice message={projectsError} />}

      {!projectFilter && !projectsError && (
        <div className="wb-card">
          <EmptyState title="还没有可查看的项目" description="接口适配任务按项目授权查看，请先在「项目」里登记一个项目。" />
        </div>
      )}

      {projectFilter && !tasks && !error && (
        <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>
      )}

      {tasks && visible.length === 0 && (
        <div className="wb-card">
          <EmptyState
            title="还没有适配任务"
            description={isAdmin ? '这个项目还没有接口适配任务；创建后系统会自动领取和执行。' : '管理员为这个项目创建适配任务后，任务会出现在这里。'}
          />
        </div>
      )}

      {tasks && visible.length > 0 && (
        <section className="wb-card wb-run-table-card">
          <div className="wb-table-wrap">
            <table className="wb-table wb-runs-table">
              <thead>
                <tr>
                  <th>任务</th><th>项目</th><th>状态</th><th>字段映射</th><th>业务调用</th><th>创建时间</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.map(task => {
                  const mapped = task.mapping_matrix.length - task.unmapped_fields.length
                  return (
                    <tr key={task.task_id}>
                      <td>
                        <Link className="wb-table-link" to={`/adaptation/${encodeURIComponent(task.task_id)}`}>
                          {task.contract.source_api.name} → {task.contract.target_adapter.name}
                        </Link>
                        <small style={{ display: 'block', color: 'var(--wb-muted)' }}>
                          {task.task_id.slice(0, 8)}　v{task.revision}
                        </small>
                      </td>
                      <td>{projectNames.get(task.project_id) ?? task.project_id}</td>
                      <td><AdaptationStatusBadge status={task.status} /></td>
                      <td>
                        {mapped}/{task.mapping_matrix.length}
                        {task.unmapped_fields.length > 0 && (
                          <span className="wb-status wb-status-warning" style={{ marginLeft: 6 }}>
                            <i aria-hidden="true" />{task.unmapped_fields.length} 待确认
                          </span>
                        )}
                      </td>
                      <td><BusinessCallSummary task={task} /></td>
                      <td><ListTime value={task.created_at} /></td>
                      <td>
                        <Link className="wb-button wb-button-secondary" to={`/adaptation/${encodeURIComponent(task.task_id)}`}>查看详情</Link>
                        {isAdmin && !createBlocked && !task.successor_id && (
                          <button
                            className="wb-button wb-button-secondary"
                            style={{ marginLeft: 8 }}
                            onClick={() => {
                              const next = new URLSearchParams(searchParams)
                              next.set('revise', task.task_id); next.delete('create')
                              setSearchParams(next, { replace: true })
                              setCreateOpen(false)
                            }}
                          >
                            导入新版本
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  )
}
