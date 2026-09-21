import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText } from './ui'
import type { PageProps } from './ui'
import { ListTime } from './presentation'
import Icon from './Icon'
import type { MaintenanceAgreementInfo, MaintenanceTaskView, PluginAvailabilityView } from './maintenance-types'
import { currentStep, maintenanceStatusLabel, maintenanceStatusTone, pluginCreateBlockedReason, stepLabel } from './maintenance-types'
import type { V3Project } from './v3-types'

// --- Create-task form ---

interface TaskDraft {
  issue_title: string
  issue_body: string
  project_id: string
  expected_behaviour: string
  delivery_goal: string
}

const blankDraft: TaskDraft = {
  issue_title: '',
  issue_body: '',
  project_id: '',
  expected_behaviour: '',
  delivery_goal: '',
}

interface ProjectBaseline {
  repository: string
  base_branch: string
  base_sha?: string
}

function projectBaseline(project: V3Project): ProjectBaseline | null {
  if (!project.repository || !project.base_branch) return null
  return { repository: project.repository, base_branch: project.base_branch }
}

function MaintenanceCreateForm({ csrfToken, onUnauthorized, projects, onCreated, onCancel }: PageProps & {
  projects: V3Project[]
  onCreated: (task: MaintenanceTaskView) => void
  onCancel: () => void
}) {
  const [draft, setDraft] = useState<TaskDraft>(blankDraft)
  const [baseSha, setBaseSha] = useState('')
  const [idempotencyKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [agreement, setAgreement] = useState<MaintenanceAgreementInfo | null>(null)
  const [agreementError, setAgreementError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  useEffect(() => () => controllerRef.current?.abort(), [])

  // 方法版本由服务端按已安装的方法包给出，前端不自己编一个约定版本。
  useEffect(() => {
    const controller = new AbortController()
    request<MaintenanceAgreementInfo>('/api/v2/maintenance/agreement', {
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

  const selectedProject = projects.find(p => String(p.id) === draft.project_id)
  const baseline = selectedProject ? projectBaseline(selectedProject) : null

  const update = <K extends keyof TaskDraft>(key: K, value: TaskDraft[K]) =>
    setDraft(current => ({ ...current, [key]: value }))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (busy) return
    if (!draft.issue_body.trim()) { setError('请填写 Issue 正文。'); return }
    if (!draft.project_id) { setError('请选择项目。'); return }
    if (!baseSha.trim() || baseSha.trim().length !== 40) { setError('请填写完整的 40 位基线 SHA。'); return }
    if (!draft.expected_behaviour.trim()) { setError('请填写期望行为。'); return }
    if (!draft.delivery_goal.trim()) { setError('请填写交付目标。'); return }
    if (!agreement) { setError('还没有拿到适用的方法版本，暂时不能提交。'); return }

    setBusy(true)
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    try {
      const body = {
        idempotency_key: idempotencyKey,
        issue: {
          source: 'manual',
          external_id: idempotencyKey,
          version: '1',
          title: draft.issue_title.trim() || draft.issue_body.trim().split(/\r?\n/)[0]?.slice(0, 120) || 'Untitled',
          body: draft.issue_body.trim(),
        },
        project_id: draft.project_id,
        repository: baseline!.repository,
        base_sha: baseSha.trim(),
        base_branch_label: baseline!.base_branch,
        expected_behaviour: draft.expected_behaviour.trim(),
        delivery_goal: draft.delivery_goal.trim(),
        agreement: { revision: agreement.revision, skill_version: agreement.skill_version },
      }
      const task = await request<MaintenanceTaskView>('/api/v2/maintenance/tasks', {
        method: 'POST',
        csrfToken,
        onUnauthorized,
        signal: controller.signal,
        body,
      })
      if (!controller.signal.aborted) {
        onCreated(task)
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
    <section className="wb-card wb-create-card" aria-labelledby="create-maintenance-title">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">创建维护任务</span>
          <h2 id="create-maintenance-title">新建维护任务</h2>
          <p>粘贴 Issue 内容，选择项目并确认基线，提交后系统将自动领取和执行。</p>
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
          Issue 标题（可选）
          <input
            maxLength={200}
            value={draft.issue_title}
            onChange={e => update('issue_title', e.target.value)}
            placeholder="留空则取正文首行"
          />
        </label>
        <label>
          Issue 正文
          <textarea
            required
            rows={6}
            value={draft.issue_body}
            onChange={e => update('issue_body', e.target.value)}
            placeholder="粘贴 Issue 的完整描述"
          />
        </label>
        <label>
          项目
          <select
            required
            value={draft.project_id}
            onChange={e => {
              update('project_id', e.target.value)
              setBaseSha('')
            }}
          >
            <option value="">请选择已注册的项目</option>
            {projects.map(p => (
              <option key={String(p.id)} value={String(p.id)}>
                {p.name}
              </option>
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
              <small>请填写仓库 {baseline.base_branch} 分支上用作本次维护基线的完整 SHA。</small>
            </label>
          </div>
        )}
        <label>
          期望行为
          <textarea
            required
            rows={3}
            value={draft.expected_behaviour}
            onChange={e => update('expected_behaviour', e.target.value)}
            placeholder="描述修复后系统应呈现的行为"
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
        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>
            取消
          </button>
          <button className="wb-button wb-button-primary" disabled={busy}>
            {busy ? '提交中…' : '创建任务'}
          </button>
        </div>
      </form>
    </section>
  )
}

// --- Task list ---

function MaintenanceStatusBadge({ status }: { status: string }) {
  const tone = maintenanceStatusTone(status as import('./maintenance-types').MaintenanceStatus)
  return (
    <span className={`wb-status wb-status-${tone}`}>
      <i aria-hidden="true" />
      {maintenanceStatusLabel(status as import('./maintenance-types').MaintenanceStatus)}
    </span>
  )
}

export default function MaintenanceTasksPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const isAdmin = user?.role !== 'member'
  const [tasks, setTasks] = useState<MaintenanceTaskView[] | null>(null)
  const [projects, setProjects] = useState<V3Project[]>([])
  const [error, setError] = useState<string | null>(null)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(() => searchParams.get('create') === '1')
  const [refreshIndex, setRefreshIndex] = useState(0)
  // 插件可用性独立于项目读取：没有可见项目时也要知道该不该提供新建入口。
  const [availability, setAvailability] = useState<PluginAvailabilityView | null>(null)
  const requested = searchParams.get('project_id') ?? ''

  // Maintenance tasks are authorized per project, so this page always reads one
  // project's tasks. Without a choice in the URL it falls back to the first
  // project the viewer can see, which keeps a plain /maintenance visit useful.
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
    request<PluginAvailabilityView>('/api/v2/maintenance/availability', {
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
    request<{ tasks: MaintenanceTaskView[]; availability?: PluginAvailabilityView }>(
      `/api/v2/maintenance/tasks?project_id=${encodeURIComponent(projectFilter)}`,
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
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [onUnauthorized, refreshIndex, projectFilter])

  const projectNames = new Map(projects.map(p => [String(p.id), p.name]))
  const visible = (tasks ?? [])
    .filter(t => !projectFilter || t.project_id === projectFilter)
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
          setTasks(null)
        }}
      >
        {projects.length === 0 && <option value="">（没有可见的项目）</option>}
        {projects.map(p => (
          <option key={String(p.id)} value={String(p.id)}>{p.name}</option>
        ))}
      </select>
    </label>
  )

  // 服务端仍然会拒绝；这里只是不把一个必然失败的按钮摆在那里。
  const createBlocked = pluginCreateBlockedReason(availability)

  return (
    <div className="wb-page">
      <PageHeader
        title="维护任务"
        description="查看和创建系统维护任务，追踪执行进度和交付。"
        actions={
          <>
            {projectPicker}
            <button
              className="wb-button wb-button-secondary"
              onClick={() => setRefreshIndex(v => v + 1)}
            >
              刷新
            </button>
            {isAdmin && !createBlocked && (
              <button
                className="wb-button wb-button-primary"
                onClick={() => setCreateOpen(v => !v)}
              >
                {createOpen ? '关闭新建' : '新建任务'}{' '}
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

      {createOpen && isAdmin && !createBlocked && (
        <MaintenanceCreateForm
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          user={user}
          projects={projects}
          onCreated={task => {
            setTasks(current => (current ? [task, ...current] : [task]))
            setCreateOpen(false)
          }}
          onCancel={() => setCreateOpen(false)}
        />
      )}

      {error && <ErrorNotice message={error} />}
      {projectsError && <ErrorNotice message={projectsError} />}

      {!projectFilter && !projectsError && (
        <div className="wb-card">
          <EmptyState
            title="还没有可查看的项目"
            description="维护任务按项目授权查看，请先在「项目」里登记一个项目。"
          />
        </div>
      )}

      {projectFilter && !tasks && !error && (
        <div className="wb-card">
          <div className="wb-list-placeholder">
            <span />
            <span />
            <span />
          </div>
        </div>
      )}

      {tasks && visible.length === 0 && (
        <div className="wb-card">
          <EmptyState
            title="还没有维护任务"
            description={
              isAdmin
                ? '这个项目还没有维护任务；创建后系统会自动领取和执行。'
                : '管理员为这个项目创建维护任务后，任务会出现在这里。'
            }
            action={
              !projectFilter && isAdmin ? (
                <button className="wb-button wb-button-primary" onClick={() => setCreateOpen(true)}>
                  创建第一个维护任务
                </button>
              ) : undefined
            }
          />
        </div>
      )}

      {tasks && visible.length > 0 && (
        <section className="wb-card wb-run-table-card">
          <div className="wb-table-wrap">
            <table className="wb-table wb-runs-table">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>项目</th>
                  <th>状态</th>
                  <th>阶段</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.map(task => (
                  <tr key={task.task_id}>
                    <td>
                      <Link className="wb-table-link" to={`/maintenance/${encodeURIComponent(task.task_id)}`}>
                        {task.issue.title || `任务 ${task.task_id.slice(0, 8)}`}
                      </Link>
                      <small style={{ display: 'block', color: 'var(--wb-muted)' }}>
                        {task.task_id.slice(0, 8)}
                      </small>
                    </td>
                    <td>{projectNames.get(task.project_id) ?? task.project_id}</td>
                    <td>
                      <MaintenanceStatusBadge status={task.status} />
                    </td>
                    <td>
                      {currentStep(task.steps)
                        ? stepLabel(currentStep(task.steps)!.step)
                        : '—'}
                    </td>
                    <td>
                      <ListTime value={task.created_at} />
                    </td>
                    <td>
                      <Link
                        className="wb-button wb-button-secondary"
                        to={`/maintenance/${encodeURIComponent(task.task_id)}`}
                      >
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
