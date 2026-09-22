import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, formatDate } from './ui'
import ClarificationPanel from './ClarificationPanel'
import type { PageProps } from './ui'
import { CopyValue, LoadingCard, ListTime } from './presentation'
import { maintenanceApi } from '../maintenance/api'
import SyntheticBadge from '../maintenance/SyntheticBadge'
import type {
  MaintenanceTaskView,
  MaintenanceEvent,
  FollowUpReceipt,
  ExportData,
  MaintenanceCheck,
} from './maintenance-types'
import {
  maintenanceStatusLabel,
  maintenanceStatusTone,
  canCancel,
  blockingTitle,
  canResume,
  stepLabel,
  stepStateLabel,
  checkHealthEvidence,
  healthEvidenceLabel,
  supplementStatus,
  supplementStatusLabel,
} from './maintenance-types'

const API_PREFIX = '/api/v2/maintenance/tasks'

// --- Sub-components ---

function StatusBadge({ status }: { status: string }) {
  const tone = maintenanceStatusTone(status as import('./maintenance-types').MaintenanceStatus)
  return (
    <span className={`wb-status wb-status-${tone}`}>
      <i aria-hidden="true" />
      {maintenanceStatusLabel(status as import('./maintenance-types').MaintenanceStatus)}
    </span>
  )
}

function StepList({ steps }: { steps: MaintenanceTaskView['steps'] }) {
  if (!steps.length) return <p className="wb-runtime-note">暂无执行阶段信息。</p>
  return (
    <ul className="wb-checklist" data-testid="step-list">
      {steps.map((step, i) => (
        <li key={i} data-state={step.state}>
          <span className="wb-checklist-label">{stepLabel(step.step)}</span>
          <span className="wb-checklist-state">{stepStateLabel(step.state)}</span>
        </li>
      ))}
    </ul>
  )
}

function CheckRow({ check }: { check: MaintenanceCheck }) {
  const evidence = checkHealthEvidence(check)
  const evidenceLabel = healthEvidenceLabel(evidence)
  const toneClass = evidence === 'current' ? 'wb-status-success' : evidence === 'stale' ? 'wb-status-warning' : 'wb-status-neutral'
  return (
    <tr>
      <td>{check.name}</td>
      <td>{check.passed ? '通过' : '未通过'}</td>
      <td>{check.exit_code}</td>
      <td>
        <span className={`wb-status ${toneClass}`} data-testid={`evidence-${evidence}`}>
          <i aria-hidden="true" />
          {evidenceLabel}
        </span>
      </td>
    </tr>
  )
}

function DeliverySection({ delivery }: { delivery: MaintenanceTaskView['delivery'] }) {
  if (!delivery) return <p className="wb-runtime-note">尚未产生交付物。</p>
  return (
    <div data-testid="delivery-section">
      <div className="wb-form-grid wb-form-grid-two" style={{ marginBottom: 16 }}>
        <div>
          <span className="wb-eyebrow">提交</span>
          <p><CopyValue value={delivery.commit} label="commit SHA" length={12} /></p>
        </div>
        <div>
          <span className="wb-eyebrow">仓库</span>
          <p>{delivery.repository}</p>
        </div>
        <div>
          <span className="wb-eyebrow">工作区基线</span>
          <p><CopyValue value={delivery.working_copy_base_sha} label="base SHA" length={12} /></p>
        </div>
        {delivery.unverified.length > 0 && (
          <div>
            <span className="wb-eyebrow">验证状态</span>
            <p><span className="wb-status wb-status-warning"><i aria-hidden="true" />未验证</span></p>
            <ul>{delivery.unverified.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </div>
        )}
      </div>
      {delivery.checks.length > 0 && (
        <div className="wb-table-wrap">
          <table className="wb-table" data-testid="checks-table">
            <thead>
              <tr>
                <th>检查</th>
                <th>结果</th>
                <th>退出码</th>
                <th>健康证据</th>
              </tr>
            </thead>
            <tbody>
              {delivery.checks.map((check, i) => (
                <CheckRow key={i} check={check} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {delivery.checks.length === 0 && <p className="wb-runtime-note">交付物没有附带检查结果。</p>}
    </div>
  )
}

// --- Follow-up form ---

function FollowUpForm({ taskId, csrfToken, onUnauthorized, onSubmitted }: {
  taskId: string
  csrfToken: string
  onUnauthorized: () => void
  onSubmitted: (receipt: FollowUpReceipt) => void
}) {
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastReceipt, setLastReceipt] = useState<FollowUpReceipt | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !content.trim()) return
    setError(null)
    setBusy(true)
    try {
      const receipt = await request<FollowUpReceipt>(
        `${API_PREFIX}/${encodeURIComponent(taskId)}/follow-up`,
        { method: 'POST', csrfToken, onUnauthorized, body: { content: content.trim() } },
      )
      setLastReceipt(receipt)
      setContent('')
      onSubmitted(receipt)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="wb-form" onSubmit={submit} data-testid="followup-form">
      <label>
        补充内容
        <textarea
          rows={3}
          value={content}
          onChange={e => setContent(e.target.value)}
          placeholder="输入对本任务的补充说明或修正"
          disabled={busy}
        />
      </label>
      {error && <ErrorNotice message={error} />}
      {lastReceipt && (
        <div className="wb-notice" role="status" data-testid="followup-receipt">
          补充状态：{supplementStatusLabel(supplementStatus(lastReceipt))}
        </div>
      )}
      <div className="wb-form-actions">
        <button className="wb-button wb-button-primary" disabled={busy || !content.trim()}>
          {busy ? '提交中…' : '提交补充'}
        </button>
      </div>
    </form>
  )
}

// --- Post-delivery feedback (new revision) ---

function FeedbackForm({ taskId, csrfToken, onUnauthorized, onCreated }: {
  taskId: string
  csrfToken: string
  onUnauthorized: () => void
  onCreated: (newTaskId: string) => void
}) {
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !content.trim()) return
    setError(null)
    setBusy(true)
    try {
      const result = await maintenanceApi.feedback(taskId, content.trim(), { csrfToken, onUnauthorized })
      onCreated(result.task_id)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="wb-form" onSubmit={submit} data-testid="feedback-form">
      <label>
        后续反馈
        <textarea
          rows={3}
          value={content}
          onChange={e => setContent(e.target.value)}
          placeholder="交付或失败后，需要继续处理的后续反馈"
          disabled={busy}
        />
      </label>
      {error && <ErrorNotice message={error} />}
      <div className="wb-form-actions">
        <button className="wb-button wb-button-primary" disabled={busy || !content.trim()}>
          {busy ? '提交中…' : '提交反馈，创建修订任务'}
        </button>
      </div>
    </form>
  )
}

// --- Event log ---

function EventLog({ events }: { events: MaintenanceEvent[] }) {
  if (!events.length) return <p className="wb-runtime-note">暂无事件记录。</p>
  return (
    <div className="wb-table-wrap" data-testid="event-log">
      <table className="wb-table">
        <thead>
          <tr>
            <th>#</th>
            <th>类型</th>
            <th>时间</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {events.map(event => (
            <tr key={event.sequence}>
              <td>{event.sequence}</td>
              <td>{event.kind}</td>
              <td>{formatDate(event.at)}</td>
              <td>
                <small style={{ wordBreak: 'break-all' }}>
                  {typeof event.payload === 'string'
                    ? event.payload
                    : JSON.stringify(event.payload)?.slice(0, 200)}
                </small>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --- Main detail page ---

export default function MaintenanceTaskDetail({ csrfToken, onUnauthorized }: PageProps) {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const revisionNotice = (location.state as { revisionNotice?: string } | null)?.revisionNotice ?? null
  const [task, setTask] = useState<MaintenanceTaskView | null>(null)
  const [events, setEvents] = useState<MaintenanceEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [exportData, setExportData] = useState<ExportData | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  const load = useCallback((manual = false) => {
    if (!taskId) return
    if (!manual && document.visibilityState !== 'visible') return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    setError(null)

    void Promise.allSettled([
      request<MaintenanceTaskView>(`${API_PREFIX}/${encodeURIComponent(taskId)}`, {
        onUnauthorized,
        signal: controller.signal,
      }),
      request<{ events: MaintenanceEvent[] }>(
        `${API_PREFIX}/${encodeURIComponent(taskId)}/events`,
        { onUnauthorized, signal: controller.signal },
      ),
    ]).then(([taskResult, eventsResult]) => {
      if (controller.signal.aborted) return
      if (taskResult.status === 'fulfilled') setTask(taskResult.value)
      else if (!(taskResult.reason instanceof WorkspaceApiError && taskResult.reason.status === 401))
        setError(errorText(taskResult.reason))
      if (eventsResult.status === 'fulfilled') setEvents(eventsResult.value.events)
    }).finally(() => {
      if (controllerRef.current === controller) {
        controllerRef.current = null
        if (!controller.signal.aborted) setLoading(false)
      }
    })
  }, [taskId, onUnauthorized])

  useEffect(() => {
    load(true)
    const timer = window.setInterval(() => load(), 8000)
    return () => {
      controllerRef.current?.abort()
      window.clearInterval(timer)
    }
  }, [load])

  const doAction = async (action: 'cancel' | 'resume' | 'approve') => {
    if (!taskId || actionBusy) return
    setActionError(null)
    setActionBusy(true)
    try {
      const updated = await request<MaintenanceTaskView>(
        `${API_PREFIX}/${encodeURIComponent(taskId)}/${action}`,
        { method: 'POST', csrfToken, onUnauthorized },
      )
      setTask(updated)
    } catch (cause) {
      setActionError(errorText(cause))
    } finally {
      setActionBusy(false)
    }
  }

  const doExport = async () => {
    if (!taskId) return
    setExportError(null)
    try {
      const data = await request<ExportData>(
        `${API_PREFIX}/${encodeURIComponent(taskId)}/export`,
        { onUnauthorized },
      )
      setExportData(data)
    } catch (cause) {
      setExportError(errorText(cause))
    }
  }

  const downloadArtifact = (name: string) => {
    if (!taskId) return
    // Direct browser download via a link — no fetch needed.
    const a = document.createElement('a')
    a.href = `${API_PREFIX}/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(name)}`
    a.download = name
    a.click()
  }

  if (!taskId) return <ErrorNotice message="缺少任务编号。" />

  // 后端在 `actions` 里给出这一刻允许的动作子集；给了就只按它渲染，不叠加旧的状态
  // 推断。没给（旧后端）时退回原来按状态/字段判断的行为，保持兼容。永远不出现 pause：
  // 执行器不支持原地暂停。
  const actions = task?.actions
  const allow = (key: string, fallback: boolean) => (actions ? actions.includes(key) : fallback)
  const showApprove = Boolean(task?.pending_plan) && allow('approve', true)
  const showResume = Boolean(task && canResume(task.status, task.resumable, task.pending_questions)) && allow('resume', true)
  const showCancel = Boolean(task && canCancel(task.status)) && allow('cancel', true)
  const showExport = Boolean(task?.delivery) && allow('export', true)
  const showAnswer = allow('answer', true)
  const showSupplement = allow('supplement', true)
  const showFeedback = allow('feedback', false)

  return (
    <div className="wb-page">
      <p className="wb-back-link" style={{ marginBottom: 12 }}>
        <Link to="/maintenance">← 返回运维监控</Link>
      </p>
      {revisionNotice && (
        <div className="wb-notice" role="status" data-testid="revision-notice">{revisionNotice}</div>
      )}
      <PageHeader
        title={task ? task.issue.title || `任务 ${task.task_id.slice(0, 8)}` : '维护任务'}
        description={task ? `任务 ${task.task_id}` : undefined}
        actions={
          <>
            <button className="wb-button wb-button-secondary" onClick={() => load(true)}>
              刷新
            </button>
            {showApprove && (
              <button
                className="wb-button wb-button-primary"
                disabled={actionBusy}
                onClick={() => void doAction('approve')}
              >
                {actionBusy ? '操作中…' : '批准计划'}
              </button>
            )}
            {showResume && (
              <button
                className="wb-button wb-button-primary"
                disabled={actionBusy}
                onClick={() => void doAction('resume')}
              >
                {actionBusy ? '操作中…' : '继续执行'}
              </button>
            )}
            {showCancel && (
              <button
                className="wb-button wb-button-secondary"
                disabled={actionBusy}
                onClick={() => void doAction('cancel')}
                style={{ color: 'var(--wb-red)' }}
              >
                {actionBusy ? '操作中…' : '取消任务'}
              </button>
            )}
          </>
        }
      />

      {error && <ErrorNotice message={error} />}
      {actionError && <ErrorNotice message={actionError} />}

      {task && showAnswer && (
        <ClarificationPanel<MaintenanceTaskView>
          questions={task.pending_questions ?? []}
          endpoint={`/api/v2/maintenance/tasks/${encodeURIComponent(taskId ?? '')}/clarify`}
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          onAnswered={updated => setTask(updated)}
        />
      )}
      {loading && !task && <LoadingCard label="正在读取任务详情" />}

      {task && (
        <>
          {/* Status and blocking reason */}
          <section className="wb-card" aria-label="任务状态" data-testid="status-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">当前状态</span>
                <h2>
                  <StatusBadge status={task.status} /> <SyntheticBadge synthetic={task.synthetic} />
                </h2>
              </div>
            </div>
            {task.blocking_reason && (
              <div className="wb-error" role="status" data-testid="blocking-reason">
                <strong>{blockingTitle(task.blocking_reason.kind)}</strong>
                {task.blocking_reason.message && (
                  <span data-testid="blocking-message">{task.blocking_reason.message}</span>
                )}
                {/* 原始种类码仍然可引用：事件日志里有对应的那条，这里也留一份。 */}
                <span className="wb-muted" data-testid="blocking-kind">
                  原始代码 <code>{task.blocking_reason.kind}</code>
                </span>
              </div>
            )}
            <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
              <div>
                <span className="wb-eyebrow">任务编号</span>
                <p><CopyValue value={task.task_id} label="任务编号" length={12} /></p>
              </div>
              <div>
                <span className="wb-eyebrow">版本</span>
                <p>v{task.revision}</p>
              </div>
              <div>
                <span className="wb-eyebrow">项目</span>
                <p>{task.project_id}</p>
              </div>
              <div>
                <span className="wb-eyebrow">创建时间</span>
                <p><ListTime value={task.created_at} /></p>
              </div>
              {task.cost_usd != null && (
                <div>
                  <span className="wb-eyebrow">费用</span>
                  <p>${task.cost_usd.toFixed(4)}</p>
                </div>
              )}
              {task.execution_id && (
                <div>
                  <span className="wb-eyebrow">执行编号</span>
                  <p><CopyValue value={task.execution_id} label="执行编号" length={12} /></p>
                </div>
              )}
            </div>
          </section>

          {/* Baseline */}
          <section className="wb-card" aria-label="基线信息">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">基线</span>
                <h2>仓库与基线</h2>
              </div>
            </div>
            <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
              <div>
                <span className="wb-eyebrow">仓库</span>
                <p>{task.baseline.repository}</p>
              </div>
              <div>
                <span className="wb-eyebrow">分支</span>
                <p>{task.baseline.base_branch_label}</p>
              </div>
              <div className="wb-span-two">
                <span className="wb-eyebrow">基线 SHA</span>
                <p><CopyValue value={task.baseline.base_sha} label="基线 SHA" length={12} /></p>
              </div>
            </div>
          </section>

          {/* Issue */}
          <section className="wb-card" aria-label="Issue 内容">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">Issue</span>
                <h2>{task.issue.title}</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
              {task.issue.body}
            </div>
          </section>

          {/* Expected behaviour and delivery goal */}
          <section className="wb-card" aria-label="期望与目标">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">工作约束</span>
                <h2>期望行为与交付目标</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>期望行为</h3>
              <p style={{ whiteSpace: 'pre-wrap', marginBottom: 16 }}>{task.expected_behaviour}</p>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>交付目标</h3>
              <p style={{ whiteSpace: 'pre-wrap' }}>{task.delivery_goal}</p>
            </div>
          </section>

          {/* Execution progress (steps) — F3: show stage only, no percentage */}
          <section className="wb-card" aria-label="执行进度" data-testid="progress-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">执行阶段</span>
                <h2>进度</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <StepList steps={task.steps} />
            </div>
          </section>

          {/* Delivery */}
          <section className="wb-card" aria-label="交付物" data-testid="delivery-card">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">交付</span>
                <h2>交付物与检查</h2>
              </div>
              {showExport && (
                <button className="wb-button wb-button-secondary" onClick={() => void doExport()}>
                  导出回执
                </button>
              )}
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <DeliverySection delivery={task.delivery} />
            </div>
          </section>

          {/* Export / download */}
          {exportError && <ErrorNotice message={exportError} />}
          {exportData && (
            <section className="wb-card" aria-label="导出数据" data-testid="export-section">
              <div className="wb-card-head">
                <div>
                  <span className="wb-eyebrow">导出</span>
                  <h2>回执与产物</h2>
                </div>
              </div>
              <div style={{ padding: '0 20px 20px' }}>
                <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, background: 'var(--wb-bg)', padding: 12, borderRadius: 6 }}>
                  {exportData.text || JSON.stringify(exportData.receipt, null, 2)}
                </pre>
                {exportData.artifacts.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>产物文件</h3>
                    <ul>
                      {exportData.artifacts.map(artifact => (
                        <li key={artifact.name} style={{ marginBottom: 4 }}>
                          <button
                            className="wb-button wb-button-secondary"
                            onClick={() => downloadArtifact(artifact.name)}
                            style={{ marginRight: 8 }}
                          >
                            下载
                          </button>
                          {artifact.name}{' '}
                          <small>({(artifact.size / 1024).toFixed(1)} KB)</small>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </section>
          )}

          {task?.pending_plan && (
        <section className="wb-card" aria-label="待批准的计划" data-testid="pending-plan">
          <div className="wb-card-head">
            <div>
              <span className="wb-eyebrow">等待批准</span>
              <h2>{task.pending_plan.title || '待批准的执行计划'}</h2>
              {task.pending_plan.summary && <p>{task.pending_plan.summary}</p>}
            </div>
          </div>
          <div style={{ padding: '0 20px 20px' }}>
            <ul className="wb-plain-list">
              {task.pending_plan.tasks.map(t => (
                <li key={t.id}>
                  <strong>{t.title}</strong>
                  {t.paths.length > 0 && <>　改动文件：{t.paths.join('、')}</>}
                  {t.checks.length > 0 && <>　检查：{t.checks.join('、')}</>}
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      {/* Follow-up supplement */}
          {showSupplement && (
          <section className="wb-card" aria-label="补充说明" data-testid="followup-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">补充</span>
                <h2>提交补充内容</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              {(task.supplements ?? []).length > 0 && (
                <ul className="wb-plain-list" data-testid="supplement-list">
                  {(task.supplements ?? []).map(s => (
                    <li key={s.id}>
                      <span className="wb-tag">{supplementStatusLabel(s.state)}</span>{' '}
                      {s.content}
                    </li>
                  ))}
                </ul>
              )}
              <FollowUpForm
                taskId={task.task_id}
                csrfToken={csrfToken}
                onUnauthorized={onUnauthorized}
                onSubmitted={() => load(true)}
              />
            </div>
          </section>
          )}

          {/* Post-delivery feedback → new revision task */}
          {showFeedback && (
          <section className="wb-card" aria-label="后续反馈" data-testid="feedback-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">反馈</span>
                <h2>后续反馈</h2>
                <p>已交付或失败后仍需处理的诉求，提交后会创建一个新修订任务，原任务保留不变。</p>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <FeedbackForm
                taskId={task.task_id}
                csrfToken={csrfToken}
                onUnauthorized={onUnauthorized}
                onCreated={newTaskId => navigate(`/maintenance/${encodeURIComponent(newTaskId)}`, {
                  state: { revisionNotice: '已创建修订任务，原任务保留' },
                })}
              />
            </div>
          </section>
          )}

          {/* Events */}
          <section className="wb-card" aria-label="事件日志">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">事件</span>
                <h2>事件日志</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <EventLog events={events} />
            </div>
          </section>
        </>
      )}
    </div>
  )
}
