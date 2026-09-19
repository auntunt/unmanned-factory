import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import {
  ENV_LABEL, SUPPORT_LABEL, TASK_STATUS_LABEL, formatBytes, packsBase,
  type PackBinding, type PackTask, type SupportRow, type TaskStatus,
} from '../workbench/pack-types'

// ---- status classification ------------------------------------------------
type FailureKind = 'error' | 'cancelled' | 'timeout' | 'unknown'

function classifyFailure(task: PackTask, timedOut: boolean): FailureKind {
  if (task.status === 'cancelled') return 'cancelled'
  if (timedOut) return 'timeout'
  if (task.status === 'failed') return 'error'
  return 'unknown'
}

const FAILURE_LABEL: Record<FailureKind, string> = {
  error: '执行失败', cancelled: '已取消', timeout: '处理超时', unknown: '状态未知',
}
const FAILURE_CSS: Record<FailureKind, string> = {
  error: 'is-bad', cancelled: 'is-cancelled', timeout: 'is-timeout', unknown: 'is-unknown',
}

// ---- capability declaration -----------------------------------------------
function SupportMatrix({ rows }: { rows: SupportRow[] }) {
  return <div className="cv-support-matrix">
    {rows.map(row => <div key={row.format} className="cv-support-row">
      <span className={`cv-support-status cv-support-${row.status}`}>{SUPPORT_LABEL[row.status]}</span>
      <span className="cv-support-format">{row.format}</span>
    </div>)}
  </div>
}

function InputConstraints({ binding }: { binding: PackBinding }) {
  const contract = binding.tool_contract
  if (!contract) return <span className="cv-capability-undeclared">能力声明未加载</span>
  const perms = contract.permissions
  const maxInput = perms?.max_input_bytes
  const timeout = contract.timeout_seconds
  return <span className="cv-capability-constraints">
    {maxInput != null ? `上限 ${formatBytes(maxInput)}` : '大小限制未声明'}
    {timeout != null ? ` · 超时 ${timeout}s` : ''}
  </span>
}

/** 日常调用交付：上传文件 → 调用已挂靠的职能包版本 → 下载成果与校验结果。
 *
 *  按职能包能力声明呈现输入范围与结果，不假设特定业务（MFD 或 CSV）。
 *  版本在提交时冻结；失败也保留候选产物并明确标注，不冒称成功。 */
export default function CapabilityPanel({ agentId, csrfToken, onUnauthorized, restoreRecent = true }: PageProps & { agentId: string; restoreRecent?: boolean }) {
  const [bindings, setBindings] = useState<PackBinding[] | null>(null)
  const [task, setTask] = useState<PackTask | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [timedOut, setTimedOut] = useState(false)
  const [showSupport, setShowSupport] = useState<string | null>(null)
  const opKey = useRef<string | null>(null)
  const gen = useRef(0)

  useEffect(() => {
    const mine = (gen.current += 1)
    const controller = new AbortController()
    setBindings(null); setTask(null); setError(null); setTimedOut(false)
    request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(agentId)}`,
      { onUnauthorized, signal: controller.signal })
      .then(value => { if (gen.current === mine) setBindings(value.bindings) })
      .catch(() => { if (gen.current === mine) setBindings([]) })
    return () => controller.abort()
  }, [agentId, onUnauthorized])

  // Restore from last invocation on mount (history recovery)
  useEffect(() => {
    if (!restoreRecent || !bindings || bindings.length === 0) return
    const mine = gen.current
    const controller = new AbortController()
    request<{ tasks: PackTask[] }>(`${packsBase}/invocations`,
      { onUnauthorized, signal: controller.signal })
      .then(res => {
        if (gen.current !== mine) return
        // Find the most recent task for any of the bound packs on this agent
        const packIds = new Set(bindings.map(b => b.pack_id))
        const recent = res.tasks.find(t => t.agent_id === agentId && packIds.has(t.pack_id))
        if (recent) setTask(recent)
      })
      .catch(() => { /* non-critical: history recovery is best-effort */ })
    return () => controller.abort()
  }, [agentId, bindings, onUnauthorized, restoreRecent])

  const poll = useCallback(async (taskId: string, mine: number) => {
    const deadline = Date.now() + 180_000
    for (;;) {
      const current = await request<PackTask>(`${packsBase}/invocations/${encodeURIComponent(taskId)}`, { onUnauthorized })
      if (gen.current !== mine) return  // switched role/conversation: never touch the new page
      setTask(current)
      if (['succeeded', 'failed', 'cancelled'].includes(current.status)) return
      if (Date.now() > deadline) {
        setTimedOut(true)
        return
      }
      await new Promise(resolve => setTimeout(resolve, 1200))
    }
  }, [onUnauthorized])

  const run = async (binding: PackBinding, event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''
    if (!file || busy) return

    // Client-side size check against declared limit
    const maxInput = binding.tool_contract?.permissions?.max_input_bytes
    if (maxInput != null && file.size > maxInput) {
      setError(`文件大小 ${formatBytes(file.size)} 超过该能力的上限 ${formatBytes(maxInput)}`)
      return
    }

    const mine = gen.current
    setBusy(true); setError(null); setTask(null); setTimedOut(false)
    if (!opKey.current) opKey.current = crypto.randomUUID()
    try {
      const body = new FormData()
      body.append('file', file); body.append('agent_id', agentId)
      body.append('pack_id', binding.pack_id); body.append('operation_key', opKey.current)
      const started = await request<PackTask>(`${packsBase}/invocations`, { method: 'POST', csrfToken, onUnauthorized, body })
      if (gen.current !== mine) return
      setTask(started)
      await poll(started.id, mine)
      opKey.current = null
    } catch (cause) { if (gen.current === mine) setError(errorText(cause)) } finally { setBusy(false) }
  }

  if (!bindings || bindings.length === 0) return null

  return <div className="cv-capability">
    {bindings.map(binding => <div key={binding.id} className="cv-capability-row">
      <div className="cv-capability-info">
        <div>
          <strong>{binding.pack_name}</strong>
          <span className="cv-capability-version"> v{binding.version}</span>
          {binding.upgrade_available && <Link className="cv-capability-link"
            to={`/ability-center/packs/${encodeURIComponent(binding.pack_id)}?tab=versions`}>可升级到 v{binding.latest_version}</Link>}
        </div>
        {binding.tool_contract?.purpose && <p className="cv-capability-purpose">{binding.tool_contract.purpose}</p>}
        <div className="cv-capability-meta">
          <InputConstraints binding={binding} />
          {binding.tool_contract?.support_matrix && binding.tool_contract.support_matrix.length > 0 && <>
            <span className="cv-capability-sep">·</span>
            <button className="cv-capability-toggle" type="button"
              onClick={() => setShowSupport(showSupport === binding.id ? null : binding.id)}>
              {showSupport === binding.id ? '收起' : '支持范围'}
            </button>
          </>}
        </div>
        {showSupport === binding.id && binding.tool_contract?.support_matrix &&
          <SupportMatrix rows={binding.tool_contract.support_matrix} />}
      </div>
      {binding.environment?.status === 'unavailable'
        ? <span className="cv-capability-warn">{ENV_LABEL.unavailable}：{binding.environment.missing?.join('、') || '未知依赖'}，
          请到 <Link className="cv-capability-link" to="/settings/runtime">模型与执行</Link> 定位后重试</span>
        : <label className="cv-attach" title={`使用 ${binding.pack_name} v${binding.version} 处理文件`}>
          <Icon name="delivery" width={16} height={16} /> {busy ? '处理中…' : '上传文件'}
          <input type="file" disabled={busy} onChange={event => void run(binding, event)} />
        </label>}
    </div>)}

    {error && <div className="cv-error" role="alert"><span>{error}</span></div>}

    {task && <TaskResult task={task} timedOut={timedOut} />}
  </div>
}

// ---- result card with four distinct failure states -------------------------
function TaskResult({ task, timedOut }: { task: PackTask; timedOut: boolean }) {
  const terminal = ['succeeded', 'failed', 'cancelled'].includes(task.status)
  const isOk = task.status === 'succeeded'
  const failKind = (!isOk && (terminal || timedOut)) ? classifyFailure(task, timedOut) : null

  const headLabel = isOk
    ? TASK_STATUS_LABEL.succeeded
    : failKind ? FAILURE_LABEL[failKind]
    : TASK_STATUS_LABEL[task.status as TaskStatus] || '处理中…'

  const cssClass = isOk ? 'is-ok' : failKind ? FAILURE_CSS[failKind] : ''

  return <div className={`cv-result ${cssClass}`}>
    <div className="cv-result-head">
      <strong>{headLabel}</strong>
      <span className="cv-capability-version">所用能力版本 v{task.snapshot.version}</span>
    </div>

    {failKind === 'error' && <p className="cv-result-reason">{task.error || '执行失败'}
      {task.error_code && <code> · {task.error_code}</code>}</p>}

    {failKind === 'cancelled' && <p className="cv-result-reason">该调用已被取消。
      {task.error && <> {task.error}</>}</p>}

    {failKind === 'timeout' && <p className="cv-result-reason">
      处理时间超过客户端等待上限，任务可能仍在后台运行。刷新页面可查看最终结果。</p>}

    {failKind === 'unknown' && <p className="cv-result-reason">
      任务状态无法确认。刷新页面或稍后重试。</p>}

    {task.diagnostics && task.diagnostics.length > 0 && <details className="cv-collapse">
      <summary>诊断信息 ({task.diagnostics.length})</summary>
      <div className="cv-collapse-body">
        {task.diagnostics.map((d, i) => <p key={i} className="cv-result-reason">{d}</p>)}
      </div>
    </details>}

    {isOk && task.result && Object.keys(task.result).length > 0 && <p className="cv-result-reason">
      {Object.entries(task.result).map(([key, value]) => `${key}：${String(value)}`).join(' · ')}</p>}

    {task.outputs.map(output => <a key={output.id} className="cv-filechip" style={{ textDecoration: 'none' }}
      href={`${packsBase}/artifacts/${encodeURIComponent(output.id)}/download`}>
      <Icon name="download" width={13} height={13} /><span>{output.name}</span>
      {output.validation_status !== 'passed' && <span className="cv-capability-warn">未通过验证</span>}
    </a>)}

    {failKind === 'error' && task.outputs.length === 0 && <p className="cv-result-reason">没有产生可下载的结果文件。</p>}
  </div>
}
