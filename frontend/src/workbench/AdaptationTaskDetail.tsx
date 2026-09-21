import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, formatDate } from './ui'
import ClarificationPanel from './ClarificationPanel'
import type { PageProps } from './ui'
import { CopyValue, LoadingCard, ListTime } from './presentation'
import type {
  AdaptationTaskView,
  AdaptationEvent,
  AdaptationCheck,
  MappingEntry,
  ExportData,
} from './adaptation-types'
import {
  adaptationStatusLabel,
  adaptationStatusTone,
  canCancelAdaptation,
  mappedRows,
  unmappedRows,
  technicalSuccessLabel,
  businessAcceptedLabel,
  businessCompletedLabel,
  businessCompletedCaveat,
  mockLabel,
  mockTone,
  triTone,
  deliveryTierLabel,
} from './adaptation-types'

const API_PREFIX = '/api/v2/adaptation/tasks'

// --- Sub-components ---

function StatusBadge({ status }: { status: AdaptationTaskView['status'] }) {
  const tone = adaptationStatusTone(status)
  return <span className={`wb-status wb-status-${tone}`}><i aria-hidden="true" />{adaptationStatusLabel(status)}</span>
}

function ContractSection({ task }: { task: AdaptationTaskView }) {
  const { source_api, target_adapter } = task.contract
  return (
    <section className="wb-card" aria-label="契约信息" data-testid="contract-section">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">契约</span><h2>来源接口与目标 Adapter</h2></div>
      </div>
      <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
        <div><span className="wb-eyebrow">来源接口</span><p>{source_api.name} v{source_api.version}</p></div>
        <div><span className="wb-eyebrow">环境</span><p data-testid="source-environment">{source_api.environment}</p></div>
        <div className="wb-span-two"><span className="wb-eyebrow">base_url</span><p>{source_api.base_url}</p></div>
        <div><span className="wb-eyebrow">鉴权方式</span><p>{source_api.auth}</p></div>
        <div><span className="wb-eyebrow">目标 Adapter</span><p>{target_adapter.name}（{target_adapter.entry}）</p></div>
        <div className="wb-span-two"><span className="wb-eyebrow">契约摘要</span><p><CopyValue value={task.contract_digest} label="契约摘要" length={16} /></p></div>
      </div>
      <div className="wb-form-grid" style={{ padding: '0 20px 20px', display: 'flex', gap: 24 }} data-testid="counts-row">
        <div><span className="wb-eyebrow">HTTP 操作数</span><p>{task.counts.http_operations}</p></div>
        <div><span className="wb-eyebrow">业务报文数</span><p>{task.counts.business_messages}</p></div>
        <div><span className="wb-eyebrow">字段数</span><p>{task.counts.fields}</p></div>
      </div>
      <div style={{ padding: '0 20px 20px' }}>
        <span className="wb-eyebrow">部署层级</span>
        <p>{deliveryTierLabel(task.delivery_tier)}</p>
        {task.synthetic && <p className="wb-muted">本任务基于合成示例契约，不代表任何客户验收案例。</p>}
      </div>
    </section>
  )
}

function ContractDiffSection({ task }: { task: AdaptationTaskView }) {
  if (!task.contract_diff) return null
  const diff = task.contract_diff
  return (
    <section className="wb-card" aria-label="契约版本变化" data-testid="diff-section">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">变更</span><h2>契约版本变化：{diff.source_api_version.old} → {diff.source_api_version.new}</h2></div>
      </div>
      <div style={{ padding: '0 20px 20px' }}>
        <p>新增字段：{diff.fields_added.join('、') || '（无）'}</p>
        <p>移除字段：{diff.fields_removed.join('、') || '（无）'}</p>
        <p>变化字段：{diff.fields_changed.join('、') || '（无）'}</p>
        {task.affected_mappings.length > 0 && (
          <>
            <h3 style={{ fontSize: 13, fontWeight: 600, margin: '12px 0 6px' }}>受影响映射</h3>
            <ul className="wb-plain-list" data-testid="affected-mappings">
              {task.affected_mappings.map((item, i) => (
                <li key={i}><strong>{item.source_field}</strong>（{item.target_field || '未映射'}）：{item.reason}</li>
              ))}
            </ul>
          </>
        )}
      </div>
    </section>
  )
}

function MappedTable({ rows }: { rows: MappingEntry[] }) {
  if (rows.length === 0) return <p className="wb-runtime-note">没有已映射的字段。</p>
  return (
    <div className="wb-table-wrap">
      <table className="wb-table" data-testid="mapped-table">
        <thead>
          <tr><th>源字段</th><th>目标字段</th><th>类型</th><th>单位</th><th>精度</th><th>转换说明</th></tr>
        </thead>
        <tbody>
          {rows.map((m, i) => (
            <tr key={i}>
              <td>{m.source_field}</td><td>{m.target_field}</td><td>{m.type || '—'}</td>
              <td>{m.unit || '—'}</td><td>{m.precision || '—'}</td><td>{m.transform || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function UnmappedList({ rows }: { rows: MappingEntry[] }) {
  if (rows.length === 0) return <p className="wb-runtime-note">没有待确认字段——所有源字段都已映射。</p>
  return (
    <ul className="wb-plain-list" data-testid="unmapped-list">
      {rows.map((m, i) => (
        <li key={i} className="wb-error" role="note" style={{ marginBottom: 8 }}>
          <strong>{m.source_field}</strong>　无法映射
          <p>影响：{m.impact || '（未说明影响）'}</p>
        </li>
      ))}
    </ul>
  )
}

function MappingMatrixSection({ task }: { task: AdaptationTaskView }) {
  const mapped = mappedRows(task)
  const unmapped = unmappedRows(task)
  return (
    <section className="wb-card" aria-label="字段映射矩阵" data-testid="mapping-section">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">映射</span><h2>字段映射矩阵（{mapped.length}/{task.mapping_matrix.length} 已映射）</h2></div>
      </div>
      <div style={{ padding: '0 20px 20px' }}>
        <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>已映射字段</h3>
        <MappedTable rows={mapped} />
      </div>
      <div
        className="wb-notice"
        role="status"
        data-testid="unmapped-section"
        style={{ margin: '0 20px 20px', padding: 16, border: '1px solid var(--wb-warning, #b45309)' }}
      >
        <h3 style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>
          待确认 / 无法映射字段（{unmapped.length}）——不能与已映射字段混同
        </h3>
        <UnmappedList rows={unmapped} />
      </div>
    </section>
  )
}

function MessageSection({ task }: { task: AdaptationTaskView }) {
  const m = task.message
  return (
    <section className="wb-card" aria-label="业务报文" data-testid="message-section">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">报文</span><h2>本次处理的业务报文</h2></div>
      </div>
      <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
        <div><span className="wb-eyebrow">名称</span><p>{m.name}</p></div>
        <div><span className="wb-eyebrow">方法与路径</span><p>{m.method} {m.path}</p></div>
        <div><span className="wb-eyebrow">是否幂等</span><p>{m.idempotent ? '是' : '否'}</p></div>
        {!m.idempotent && <div><span className="wb-eyebrow">重试/去重策略</span><p>{m.retry_policy || '（未说明）'}</p></div>}
      </div>
    </section>
  )
}

function TriRow({ label, tone, value }: { label: string; tone: ReturnType<typeof triTone>; value: string }) {
  return (
    <div>
      <span className="wb-eyebrow">{label}</span>
      <p><span className={`wb-status wb-status-${tone}`}><i aria-hidden="true" />{value}</span></p>
    </div>
  )
}

function BusinessCallSection({ task }: { task: AdaptationTaskView }) {
  const call = task.business_call
  return (
    <section className="wb-card" aria-label="业务调用结果" data-testid="business-call-section">
      <div className="wb-card-head">
        <div><span className="wb-eyebrow">联调</span><h2>业务调用结果</h2></div>
      </div>
      <div style={{ padding: '0 20px 20px' }}>
        {!call && <p className="wb-runtime-note" data-testid="no-business-call">未联调，没有可引用的真实业务报文结果。</p>}
        {call && (
          <>
            <div style={{ marginBottom: 12 }}>
              <span
                className={`wb-status wb-status-${mockTone(call)}`}
                data-testid="mock-marker"
              >
                <i aria-hidden="true" />{mockLabel(call)}
              </span>
            </div>
            <div className="wb-form-grid wb-form-grid-two" data-testid="tri-state-grid">
              <TriRow label="技术是否成功" tone={triTone(call.technical_success)} value={technicalSuccessLabel(call)} />
              <TriRow label="业务是否受理" tone={triTone(call.business_accepted)} value={businessAcceptedLabel(call)} />
              <TriRow label="业务是否最终完成" tone={triTone(call.business_completed)} value={businessCompletedLabel(call)} />
              <div><span className="wb-eyebrow">请求关联 ID</span><p>{call.correlation_id || '—'}</p></div>
              <div className="wb-span-two"><span className="wb-eyebrow">端点</span><p>{call.endpoint || '—'}</p></div>
            </div>
            {businessCompletedCaveat(call) && (
              <p className="wb-muted" data-testid="business-completed-caveat" style={{ marginTop: 8 }}>
                {businessCompletedCaveat(call)}
              </p>
            )}
            <div style={{ marginTop: 12 }}>
              <span className="wb-eyebrow">脱敏响应</span>
              <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, background: 'var(--wb-bg)', padding: 12, borderRadius: 6 }}>
                {JSON.stringify(call.sanitized_response ?? {}, null, 2)}
              </pre>
            </div>
          </>
        )}
      </div>
    </section>
  )
}

function CheckRow({ check }: { check: AdaptationCheck }) {
  return (
    <tr>
      <td>{check.name}</td>
      <td>{check.passed ? '通过' : '未通过'}</td>
      <td>{check.exit_code}</td>
    </tr>
  )
}

function DeliverySection({ delivery }: { delivery: AdaptationTaskView['delivery'] }) {
  if (!delivery) return <p className="wb-runtime-note">尚未产生交付物。</p>
  return (
    <div data-testid="delivery-section">
      <div className="wb-form-grid wb-form-grid-two" style={{ marginBottom: 16 }}>
        <div><span className="wb-eyebrow">提交</span><p><CopyValue value={delivery.commit} label="commit SHA" length={12} /></p></div>
        <div><span className="wb-eyebrow">仓库</span><p>{delivery.repository}</p></div>
        <div><span className="wb-eyebrow">工作区基线</span><p>{delivery.working_copy_base_sha ? <CopyValue value={delivery.working_copy_base_sha} label="base SHA" length={12} /> : '—'}</p></div>
        {delivery.unverified.length > 0 && (
          <div><span className="wb-eyebrow">未验证项</span><p>{delivery.unverified.join('、')}</p></div>
        )}
      </div>
      {delivery.checks.length > 0 ? (
        <div className="wb-table-wrap">
          <table className="wb-table" data-testid="checks-table">
            <thead><tr><th>检查</th><th>结果</th><th>退出码</th></tr></thead>
            <tbody>{delivery.checks.map((check, i) => <CheckRow key={i} check={check} />)}</tbody>
          </table>
        </div>
      ) : <p className="wb-runtime-note">交付物没有附带检查结果。</p>}
    </div>
  )
}

// --- Main detail page ---

export default function AdaptationTaskDetail({ csrfToken, onUnauthorized }: PageProps) {
  const { taskId } = useParams<{ taskId: string }>()
  const [task, setTask] = useState<AdaptationTaskView | null>(null)
  const [events, setEvents] = useState<AdaptationEvent[]>([])
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
      request<AdaptationTaskView>(`${API_PREFIX}/${encodeURIComponent(taskId)}`, { onUnauthorized, signal: controller.signal }),
      request<{ events: AdaptationEvent[] }>(`${API_PREFIX}/${encodeURIComponent(taskId)}/events`, { onUnauthorized, signal: controller.signal }),
    ]).then(([taskResult, eventsResult]) => {
      if (controller.signal.aborted) return
      if (taskResult.status === 'fulfilled') setTask(taskResult.value)
      else if (!(taskResult.reason instanceof WorkspaceApiError && taskResult.reason.status === 401)) setError(errorText(taskResult.reason))
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
    return () => { controllerRef.current?.abort(); window.clearInterval(timer) }
  }, [load])

  const doCancel = async () => {
    if (!taskId || actionBusy) return
    setActionError(null)
    setActionBusy(true)
    try {
      const updated = await request<AdaptationTaskView>(`${API_PREFIX}/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', csrfToken, onUnauthorized })
      setTask(updated)
    } catch (cause) {
      setActionError(errorText(cause))
    } finally {
      setActionBusy(false)
    }
  }

  const doApprove = async () => {
    if (!taskId || actionBusy) return
    setActionError(null)
    setActionBusy(true)
    try {
      const updated = await request<AdaptationTaskView>(
        `${API_PREFIX}/${encodeURIComponent(taskId)}/approve`,
        { method: 'POST', csrfToken, onUnauthorized })
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
      const data = await request<ExportData>(`${API_PREFIX}/${encodeURIComponent(taskId)}/export`, { onUnauthorized })
      setExportData(data)
    } catch (cause) {
      setExportError(errorText(cause))
    }
  }

  const downloadArtifact = (name: string) => {
    if (!taskId) return
    const a = document.createElement('a')
    a.href = `${API_PREFIX}/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(name)}`
    a.download = name
    a.click()
  }

  if (!taskId) return <ErrorNotice message="缺少任务编号。" />

  return (
    <div className="wb-page">
      <PageHeader
        title={task ? `${task.contract.source_api.name} → ${task.contract.target_adapter.name}` : '接口适配任务'}
        description={task ? `任务 ${task.task_id}（修订 ${task.revision}）` : undefined}
        actions={
          <>
            <button className="wb-button wb-button-secondary" onClick={() => load(true)}>刷新</button>
            {task && !task.successor_id && (
              <Link className="wb-button wb-button-secondary" to={`/adaptation?revise=${encodeURIComponent(task.task_id)}`}>
                导入下一版契约
              </Link>
            )}
            {task && task.pending_plan && (
              <button
                className="wb-button wb-button-primary"
                disabled={actionBusy}
                onClick={() => void doApprove()}
              >
                {actionBusy ? '操作中…' : '批准计划'}
              </button>
            )}
            {task && task.status === 'delivered' && (
              <button className="wb-button wb-button-secondary" onClick={() => void doExport()}>导出回执</button>
            )}
            {task && canCancelAdaptation(task.status) && (
              <button
                className="wb-button wb-button-secondary"
                disabled={actionBusy}
                onClick={() => void doCancel()}
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

      {task && (
        <ClarificationPanel<AdaptationTaskView>
          questions={task.pending_questions ?? []}
          endpoint={`/api/v2/adaptation/tasks/${encodeURIComponent(taskId ?? '')}/clarify`}
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          onAnswered={updated => setTask(updated)}
        />
      )}

      {task?.pending_plan && (
        <section className="wb-card" data-testid="pending-plan">
          <h3>等待批准的计划：{task.pending_plan.title}</h3>
          {task.pending_plan.summary && <p className="wb-muted">{task.pending_plan.summary}</p>}
          <ul className="wb-plain-list">
            {task.pending_plan.tasks.map((t, i) => (
              <li key={t.id ?? i}>
                {t.title}
                {t.paths.length > 0 && <> · 文件 {t.paths.join('、')}</>}
                {t.checks.length > 0 && <> · 检查 {t.checks.join('、')}</>}
              </li>
            ))}
          </ul>
          <p className="wb-muted">批准之后执行器才会开始改动文件。</p>
        </section>
      )}
      {loading && !task && <LoadingCard label="正在读取任务详情" />}

      {task && (
        <>
          <section className="wb-card" aria-label="任务状态" data-testid="status-section">
            <div className="wb-card-head">
              <div><span className="wb-eyebrow">当前状态</span><h2><StatusBadge status={task.status} /></h2></div>
            </div>
            <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
              <div><span className="wb-eyebrow">任务编号</span><p><CopyValue value={task.task_id} label="任务编号" length={12} /></p></div>
              <div><span className="wb-eyebrow">修订</span><p>v{task.revision}</p></div>
              <div><span className="wb-eyebrow">项目</span><p>{task.project_id}</p></div>
              <div><span className="wb-eyebrow">创建时间</span><p><ListTime value={task.created_at} /></p></div>
              {task.cost_usd != null && <div><span className="wb-eyebrow">费用</span><p>${task.cost_usd.toFixed(4)}</p></div>}
              {task.execution_id && <div><span className="wb-eyebrow">执行编号</span><p><CopyValue value={task.execution_id} label="执行编号" length={12} /></p></div>}
              {task.predecessor_id && (
                <div><span className="wb-eyebrow">上一修订</span><p><Link to={`/adaptation/${encodeURIComponent(task.predecessor_id)}`}>{task.predecessor_id.slice(0, 8)}</Link></p></div>
              )}
              {task.successor_id && (
                <div><span className="wb-eyebrow">下一修订</span><p><Link to={`/adaptation/${encodeURIComponent(task.successor_id)}`}>{task.successor_id.slice(0, 8)}</Link></p></div>
              )}
            </div>
          </section>

          <section className="wb-card" aria-label="基线信息">
            <div className="wb-card-head">
              <div><span className="wb-eyebrow">基线</span><h2>仓库与基线</h2></div>
            </div>
            <div className="wb-form-grid wb-form-grid-two" style={{ padding: '0 20px 20px' }}>
              <div><span className="wb-eyebrow">仓库</span><p>{task.baseline.repository}</p></div>
              <div><span className="wb-eyebrow">分支</span><p>{task.baseline.base_branch_label}</p></div>
              <div className="wb-span-two"><span className="wb-eyebrow">基线 SHA</span><p><CopyValue value={task.baseline.base_sha} label="基线 SHA" length={12} /></p></div>
            </div>
          </section>

          <ContractSection task={task} />
          <ContractDiffSection task={task} />
          <MappingMatrixSection task={task} />
          <MessageSection task={task} />

          <section className="wb-card" aria-label="期望与目标">
            <div className="wb-card-head">
              <div><span className="wb-eyebrow">工作约束</span><h2>期望行为与交付目标</h2></div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>期望行为</h3>
              <p style={{ whiteSpace: 'pre-wrap', marginBottom: 16 }}>{task.expected_behaviour}</p>
              <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>交付目标</h3>
              <p style={{ whiteSpace: 'pre-wrap' }}>{task.delivery_goal}</p>
            </div>
          </section>

          <BusinessCallSection task={task} />

          <section className="wb-card" aria-label="交付物" data-testid="delivery-card">
            <div className="wb-card-head">
              <div><span className="wb-eyebrow">交付</span><h2>交付物与检查</h2></div>
            </div>
            <div style={{ padding: '0 20px 20px' }}><DeliverySection delivery={task.delivery} /></div>
          </section>

          {exportError && <ErrorNotice message={exportError} />}
          {exportData && (
            <section className="wb-card" aria-label="导出数据" data-testid="export-section">
              <div className="wb-card-head">
                <div><span className="wb-eyebrow">导出</span><h2>回执与产物</h2></div>
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
                          <button className="wb-button wb-button-secondary" onClick={() => downloadArtifact(artifact.name)} style={{ marginRight: 8 }}>下载</button>
                          {artifact.name} <small>({(artifact.size / 1024).toFixed(1)} KB)</small>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </section>
          )}

          <section className="wb-card" aria-label="事件日志">
            <div className="wb-card-head">
              <div><span className="wb-eyebrow">事件</span><h2>事件日志</h2></div>
            </div>
            <div style={{ padding: '0 20px 20px' }}>
              {events.length === 0 ? <p className="wb-runtime-note">暂无事件记录。</p> : (
                <div className="wb-table-wrap" data-testid="event-log">
                  <table className="wb-table">
                    <thead><tr><th>#</th><th>类型</th><th>时间</th><th>详情</th></tr></thead>
                    <tbody>
                      {events.map(event => (
                        <tr key={event.sequence}>
                          <td>{event.sequence}</td>
                          <td>{event.kind}</td>
                          <td>{formatDate(event.at)}</td>
                          <td><small style={{ wordBreak: 'break-all' }}>{typeof event.payload === 'string' ? event.payload : JSON.stringify(event.payload)?.slice(0, 200)}</small></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
