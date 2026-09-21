import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, formatDate } from './ui'
import type { PageProps } from './ui'
import { CopyValue, LoadingCard, ListTime } from './presentation'
import type {
  ModernizationSliceView,
  ModernizationEvent,
  ExportData,
  ModernizationCheck,
  PluginAvailabilityView,
} from './modernization-types'
import {
  DIMENSIONS,
  modernizationStatusLabel,
  modernizationStatusTone,
  canCancel,
  nextAction,
  blockingTitle,
  stepLabel,
  stepStateLabel,
  checkHealthEvidence,
  healthEvidenceLabel,
  dimensionLabel,
  pluginCreateBlockedReason,
} from './modernization-types'

const API_PREFIX = '/api/v2/modernization/slices'

// --- Sub-components ---

function StatusBadge({ status }: { status: string }) {
  const tone = modernizationStatusTone(status as import('./modernization-types').ModernizationStatus)
  return (
    <span className={`wb-status wb-status-${tone}`}>
      <i aria-hidden="true" />
      {modernizationStatusLabel(status as import('./modernization-types').ModernizationStatus)}
    </span>
  )
}

function StepList({ steps }: { steps: ModernizationSliceView['steps'] }) {
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

function CheckRow({ check }: { check: ModernizationCheck }) {
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

function DeliverySection({ delivery }: { delivery: ModernizationSliceView['delivery'] }) {
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
          <p>{delivery.working_copy_base_sha
            ? <CopyValue value={delivery.working_copy_base_sha} label="base SHA" length={12} />
            : '（无法确认）'}</p>
        </div>
      </div>
      {delivery.unverified.length > 0 && (
        <div className="wb-error" role="status" data-testid="unverified-list" style={{ marginBottom: 16 }}>
          <strong>未在真实目标环境验证的条件</strong>
          <ul className="wb-plain-list">
            {delivery.unverified.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        </div>
      )}
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
              {delivery.checks.map((check, i) => <CheckRow key={i} check={check} />)}
            </tbody>
          </table>
        </div>
      )}
      {delivery.checks.length === 0 && <p className="wb-runtime-note">交付物没有附带检查结果。</p>}
    </div>
  )
}

// --- Revise (继续反馈：以新修订提交，保留旧结果) ---

function ReviseForm({ slice, csrfToken, onUnauthorized, onRevised, onCancel }: {
  slice: ModernizationSliceView
  csrfToken: string
  onUnauthorized: () => void
  onRevised: (slice: ModernizationSliceView) => void
  onCancel: () => void
}) {
  const [dimension, setDimension] = useState(slice.dimension)
  const [scopePaths, setScopePaths] = useState(slice.scope_paths.join('\n'))
  const [baseSha, setBaseSha] = useState(slice.baseline.base_sha)
  const [expectedBehaviour, setExpectedBehaviour] = useState(slice.expected_behaviour)
  const [deliveryGoal, setDeliveryGoal] = useState(slice.delivery_goal)
  const [knownGaps, setKnownGaps] = useState(slice.known_gaps.join('\n'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    const scope = scopePaths.split('\n').map(s => s.trim()).filter(Boolean)
    if (scope.length === 0) { setError('请给出至少一个具体范围路径。'); return }
    if (!baseSha.trim() || baseSha.trim().length !== 40) { setError('请填写完整的 40 位基线 SHA。'); return }
    if (!expectedBehaviour.trim()) { setError('请填写期望行为。'); return }
    if (!deliveryGoal.trim()) { setError('请填写交付目标。'); return }
    setBusy(true)
    try {
      const body = {
        idempotency_key: crypto.randomUUID(),
        project_id: slice.project_id,
        repository: slice.baseline.repository,
        base_sha: baseSha.trim(),
        base_branch_label: slice.baseline.base_branch_label,
        dimension,
        scope_paths: scope,
        expected_behaviour: expectedBehaviour.trim(),
        delivery_goal: deliveryGoal.trim(),
        delivery_tier: slice.delivery_tier,
        known_gaps: knownGaps.split('\n').map(s => s.trim()).filter(Boolean),
        agreement: slice.agreement,
      }
      const revised = await request<ModernizationSliceView>(
        `${API_PREFIX}/${encodeURIComponent(slice.slice_id)}/revise`,
        { method: 'POST', csrfToken, onUnauthorized, body },
      )
      onRevised(revised)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="wb-form" onSubmit={submit} data-testid="revise-form">
      <label>
        改造维度
        <select value={dimension} onChange={e => setDimension(e.target.value as ModernizationSliceView['dimension'])}>
          {DIMENSIONS.map(dim => <option key={dim} value={dim}>{dimensionLabel(dim)}</option>)}
        </select>
      </label>
      <label>
        基线 SHA（完整 40 位；若有更新的基线请在这里填写）
        <input
          maxLength={40}
          minLength={40}
          pattern="[0-9a-f]{40}"
          value={baseSha}
          onChange={e => setBaseSha(e.target.value.toLowerCase().replace(/[^0-9a-f]/g, ''))}
        />
      </label>
      <label>
        范围路径（每行一个）
        <textarea rows={3} value={scopePaths} onChange={e => setScopePaths(e.target.value)} />
      </label>
      <label>
        期望行为
        <textarea rows={3} value={expectedBehaviour} onChange={e => setExpectedBehaviour(e.target.value)} />
      </label>
      <label>
        交付目标
        <textarea rows={2} value={deliveryGoal} onChange={e => setDeliveryGoal(e.target.value)} />
      </label>
      <label>
        已知未验证条件（每行一个，可选）
        <textarea rows={2} value={knownGaps} onChange={e => setKnownGaps(e.target.value)} />
      </label>
      {error && <ErrorNotice message={error} />}
      <div className="wb-form-actions">
        <button type="button" className="wb-button wb-button-secondary" onClick={onCancel}>取消</button>
        <button className="wb-button wb-button-primary" disabled={busy}>
          {busy ? '提交中…' : '提交新修订'}
        </button>
      </div>
    </form>
  )
}

// --- Event log ---

function EventLog({ events }: { events: ModernizationEvent[] }) {
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
                  {typeof event.payload === 'string' ? event.payload : JSON.stringify(event.payload)?.slice(0, 200)}
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

export default function ModernizationSliceDetail({ csrfToken, onUnauthorized }: PageProps) {
  const { sliceId } = useParams<{ sliceId: string }>()
  const navigate = useNavigate()
  const [slice, setSlice] = useState<ModernizationSliceView | null>(null)
  const [events, setEvents] = useState<ModernizationEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [exportData, setExportData] = useState<ExportData | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [reviseOpen, setReviseOpen] = useState(false)
  const [availability, setAvailability] = useState<PluginAvailabilityView | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  const load = useCallback((manual = false) => {
    if (!sliceId) return
    if (!manual && document.visibilityState !== 'visible') return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    setError(null)

    void Promise.allSettled([
      request<ModernizationSliceView>(`${API_PREFIX}/${encodeURIComponent(sliceId)}`, {
        onUnauthorized, signal: controller.signal,
      }),
      request<{ events: ModernizationEvent[] }>(
        `${API_PREFIX}/${encodeURIComponent(sliceId)}/events`,
        { onUnauthorized, signal: controller.signal },
      ),
    ]).then(([sliceResult, eventsResult]) => {
      if (controller.signal.aborted) return
      if (sliceResult.status === 'fulfilled') setSlice(sliceResult.value)
      else if (!(sliceResult.reason instanceof WorkspaceApiError && sliceResult.reason.status === 401))
        setError(errorText(sliceResult.reason))
      if (eventsResult.status === 'fulfilled') setEvents(eventsResult.value.events)
    }).finally(() => {
      if (controllerRef.current === controller) {
        controllerRef.current = null
        if (!controller.signal.aborted) setLoading(false)
      }
    })
  }, [sliceId, onUnauthorized])

  useEffect(() => {
    load(true)
    const timer = window.setInterval(() => load(), 8000)
    return () => {
      controllerRef.current?.abort()
      window.clearInterval(timer)
    }
  }, [load])

  useEffect(() => {
    const controller = new AbortController()
    request<PluginAvailabilityView>('/api/v2/modernization/availability', {
      onUnauthorized, signal: controller.signal,
    })
      .then(result => { if (!controller.signal.aborted) setAvailability(result) })
      .catch(() => { /* 读不到可用性就保持当前入口，真正的拒绝在服务端 */ })
    return () => controller.abort()
  }, [onUnauthorized])

  const doAction = async (action: 'cancel' | 'resume' | 'approve') => {
    if (!sliceId || actionBusy) return
    setActionError(null)
    setActionBusy(true)
    try {
      const updated = await request<ModernizationSliceView>(
        `${API_PREFIX}/${encodeURIComponent(sliceId)}/${action}`,
        { method: 'POST', csrfToken, onUnauthorized },
      )
      setSlice(updated)
    } catch (cause) {
      setActionError(errorText(cause))
    } finally {
      setActionBusy(false)
    }
  }

  const doExport = async () => {
    if (!sliceId) return
    setExportError(null)
    try {
      const data = await request<ExportData>(`${API_PREFIX}/${encodeURIComponent(sliceId)}/export`, { onUnauthorized })
      setExportData(data)
    } catch (cause) {
      setExportError(errorText(cause))
    }
  }

  const downloadArtifact = (name: string) => {
    if (!sliceId) return
    const a = document.createElement('a')
    a.href = `${API_PREFIX}/${encodeURIComponent(sliceId)}/artifacts/${encodeURIComponent(name)}`
    a.download = name
    a.click()
  }

  if (!sliceId) return <ErrorNotice message="缺少改造切片编号。" />

  const action = slice ? nextAction(slice.status, slice.blocking_reason) : null
  const reviseBlocked = pluginCreateBlockedReason(availability)

  return (
    <div className="wb-page">
      <PageHeader
        title={slice ? `${dimensionLabel(slice.dimension)} · ${slice.slice_id.slice(0, 8)}` : '改造切片'}
        description={slice ? `切片 ${slice.slice_id}` : undefined}
        actions={
          <>
            <button className="wb-button wb-button-secondary" onClick={() => load(true)}>刷新</button>
            {slice && action === 'approve' && (
              <button className="wb-button wb-button-primary" disabled={actionBusy} onClick={() => void doAction('approve')}>
                {actionBusy ? '操作中…' : '批准计划'}
              </button>
            )}
            {slice && action === 'resume' && (
              <button className="wb-button wb-button-primary" disabled={actionBusy} onClick={() => void doAction('resume')}>
                {actionBusy ? '操作中…' : '继续执行'}
              </button>
            )}
            {slice && canCancel(slice.status) && (
              <button className="wb-button wb-button-secondary" disabled={actionBusy} onClick={() => void doAction('cancel')} style={{ color: 'var(--wb-red)' }}>
                {actionBusy ? '操作中…' : '取消切片'}
              </button>
            )}
          </>
        }
      />

      {error && <ErrorNotice message={error} />}
      {actionError && <ErrorNotice message={actionError} />}
      {loading && !slice && <LoadingCard label="正在读取切片详情" />}

      {slice && (
        <>
          <section className="wb-card" aria-label="切片状态" data-testid="status-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">当前状态</span>
                <h2><StatusBadge status={slice.status} /></h2>
              </div>
            </div>
            {slice.blocking_reason && (
              <div className="wb-error" role="status" data-testid="blocking-reason">
                <strong>{blockingTitle(slice.blocking_reason.kind)}</strong>
                {slice.blocking_reason.message && (
                  <span data-testid="blocking-message">{slice.blocking_reason.message}</span>
                )}
                <span className="wb-muted" data-testid="blocking-kind">
                  原始代码 <code>{slice.blocking_reason.kind}</code>
                </span>
              </div>
            )}
            <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
              <div>
                <span className="wb-eyebrow">切片编号</span>
                <p><CopyValue value={slice.slice_id} label="切片编号" length={12} /></p>
              </div>
              <div>
                <span className="wb-eyebrow">版本</span>
                <p>v{slice.revision}</p>
              </div>
              <div>
                <span className="wb-eyebrow">项目</span>
                <p>{slice.project_id}</p>
              </div>
              <div>
                <span className="wb-eyebrow">创建时间</span>
                <p><ListTime value={slice.created_at} /></p>
              </div>
              {slice.cost_usd != null && (
                <div>
                  <span className="wb-eyebrow">费用</span>
                  <p>${slice.cost_usd.toFixed(4)}</p>
                </div>
              )}
              {slice.execution_id && (
                <div>
                  <span className="wb-eyebrow">执行编号</span>
                  <p><CopyValue value={slice.execution_id} label="执行编号" length={12} /></p>
                </div>
              )}
              {slice.predecessor_id && (
                <div>
                  <span className="wb-eyebrow">上一修订</span>
                  <p><Link to={`/modernization/${encodeURIComponent(slice.predecessor_id)}`}>{slice.predecessor_id.slice(0, 8)}</Link></p>
                </div>
              )}
              {slice.successor_id && (
                <div>
                  <span className="wb-eyebrow">后续修订</span>
                  <p><Link to={`/modernization/${encodeURIComponent(slice.successor_id)}`}>{slice.successor_id.slice(0, 8)}</Link></p>
                </div>
              )}
            </div>
          </section>

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
                <p>{slice.baseline.repository}</p>
              </div>
              <div>
                <span className="wb-eyebrow">分支</span>
                <p>{slice.baseline.base_branch_label}</p>
              </div>
              <div className="wb-span-two">
                <span className="wb-eyebrow">基线 SHA</span>
                <p><CopyValue value={slice.baseline.base_sha} label="基线 SHA" length={12} /></p>
              </div>
            </div>
          </section>

          <section className="wb-card" aria-label="范围与代码定位">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">范围</span>
                <h2>改造范围与代码定位</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>范围路径</h3>
              <ul className="wb-plain-list">
                {slice.scope_paths.map((path, i) => <li key={i}>{path}</li>)}
              </ul>
              {slice.code_locations.length > 0 && (
                <>
                  <h3 style={{ fontSize: 13, fontWeight: 600, margin: '12px 0 6px' }}>代码定位（带来源）</h3>
                  <ul className="wb-plain-list" data-testid="code-locations">
                    {slice.code_locations.map((loc, i) => (
                      <li key={i}>{loc.path}{loc.line ? `:${loc.line}` : ''} <span className="wb-muted">（{loc.source}）</span></li>
                    ))}
                  </ul>
                </>
              )}
              {slice.known_gaps.length > 0 && (
                <>
                  <h3 style={{ fontSize: 13, fontWeight: 600, margin: '12px 0 6px' }}>已知未验证条件</h3>
                  <ul className="wb-plain-list">
                    {slice.known_gaps.map((gap, i) => <li key={i}>{gap}</li>)}
                  </ul>
                </>
              )}
            </div>
          </section>

          <section className="wb-card" aria-label="期望与目标">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">工作约束</span>
                <h2>期望行为与交付目标</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>期望行为</h3>
              <p style={{ whiteSpace: 'pre-wrap', marginBottom: 16 }}>{slice.expected_behaviour}</p>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>交付目标</h3>
              <p style={{ whiteSpace: 'pre-wrap' }}>{slice.delivery_goal}</p>
            </div>
          </section>

          <section className="wb-card" aria-label="执行进度" data-testid="progress-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">执行阶段</span>
                <h2>进度</h2>
              </div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <StepList steps={slice.steps} />
            </div>
          </section>

          <section className="wb-card" aria-label="交付物" data-testid="delivery-card">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">交付</span>
                <h2>交付物与检查</h2>
              </div>
              {slice.delivery && (
                <button className="wb-button wb-button-secondary" onClick={() => void doExport()}>导出回执</button>
              )}
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <DeliverySection delivery={slice.delivery} />
            </div>
          </section>

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
                    <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>构建物文件</h3>
                    <ul>
                      {exportData.artifacts.map(artifact => (
                        <li key={artifact.name} style={{ marginBottom: 4 }}>
                          <button className="wb-button wb-button-secondary" onClick={() => downloadArtifact(artifact.name)} style={{ marginRight: 8 }}>
                            下载
                          </button>
                          {artifact.name} <small>({(artifact.size / 1024).toFixed(1)} KB)</small>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </section>
          )}

          <section className="wb-card" aria-label="继续反馈" data-testid="revise-section">
            <div className="wb-card-head">
              <div>
                <span className="wb-eyebrow">继续反馈</span>
                <h2>补充要求</h2>
                <p>后续要求形成新修订，保留这一条的结果与已确认约束；不会覆盖当前切片。</p>
              </div>
              {!reviseOpen && !reviseBlocked && (
                <button className="wb-button wb-button-primary" onClick={() => setReviseOpen(true)}>提交修订</button>
              )}
            </div>
            {reviseBlocked && (
              <div className="wb-notice" role="status" style={{ margin: '0 20px 20px' }}>{reviseBlocked}</div>
            )}
            {reviseOpen && (
              <div style={{ padding: '0 20px 20px' }}>
                <ReviseForm
                  slice={slice}
                  csrfToken={csrfToken}
                  onUnauthorized={onUnauthorized}
                  onCancel={() => setReviseOpen(false)}
                  onRevised={revised => navigate(`/modernization/${encodeURIComponent(revised.slice_id)}`)}
                />
              </div>
            )}
          </section>

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
