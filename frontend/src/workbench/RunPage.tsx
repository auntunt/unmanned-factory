import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import RunKnowledge from '../workspace/RunKnowledge'
import TaskGraph from '../workspace/TaskGraph'
import type { AuditEvent, ConversationMessage, PlanTask, Run, RunStatus, TaskStatus } from '../workspace/types'
import type { RuntimeLimits, RuntimeProfiles } from './runtime-types'
import { StopReason } from './StopReason'
import PlanVersions from './PlanVersions'
import { RUN_VIEWS, canGenerateNextPlan, frozenPolicy, runGuidance, runView, type RunView } from './run-guidance'
import { checkPassed, executionFailure } from './run-guidance'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge, statusLabel, errorText, type PageProps } from './ui'
import './detail.css'
import { notifyDataChanged, subscribeDataRefresh } from './data-refresh'
import './run-guidance.css'
import RunJourney from './RunJourney'
import Deliverables from './Deliverables'

type RunPageProps = PageProps

const ACTIVE_STATUSES: RunStatus[] = ['received', 'planning', 'needs_clarification', 'awaiting_approval', 'queued', 'running', 'verifying', 'needs_human', 'ready_for_review']
const taskStatuses = new Set<TaskStatus>(['pending', 'queued', 'running', 'verified', 'completed', 'failed', 'blocked', 'cancelled'])
const runtimeRoles = ['planner', 'cheap', 'standard', 'strong'] as const

interface FrozenRuntimeConfiguration {
  revision?: number
  configuration_revision?: number
  profiles?: Partial<RuntimeProfiles>
  limits?: Partial<RuntimeLimits>
}

type RunWithFrozenRuntime = Run & { runtime_configuration?: FrozenRuntimeConfiguration | null }

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function taskStatusMap(value: unknown): Map<string, TaskStatus> {
  const map = new Map<string, TaskStatus>()
  if (!Array.isArray(value)) return map
  value.forEach((item) => {
    if (!isRecord(item) || typeof item.id !== 'string' || typeof item.status !== 'string') return
    if (taskStatuses.has(item.status as TaskStatus)) map.set(item.id, item.status as TaskStatus)
  })
  return map
}

function safeGithubPr(value: unknown): value is string {
  if (typeof value !== 'string') return false
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname === 'github.com' && /^\/[^/]+\/[^/]+\/pull\/\d+\/?$/.test(url.pathname)
  } catch {
    return false
  }
}

function shortSha(value: unknown): string {
  if (typeof value !== 'string' || !value) return '—'
  return value.length > 16 ? `${value.slice(0, 12)}…` : value
}

function eventSummary(value: unknown): string {
  if (typeof value === 'string') return value
  if (!isRecord(value)) return value == null ? '' : '已记录'
  const preferred = ['message', 'summary', 'outcome', 'reason', 'error', 'status', 'model']
  const pieces = preferred.filter((key) => typeof value[key] === 'string' || typeof value[key] === 'number').map((key) => `${key}：${String(value[key])}`)
  return pieces.length ? pieces.join(' · ') : '已记录一条审计事实'
}

const eventTypeLabels: Record<string, string> = {
  'quota.reserved': '额度已预留',
  'quota.settled': '额度已结算',
}

function eventTypeLabel(type: string): string {
  return eventTypeLabels[type] ?? type
}

function hasFailureEvidence(event: AuditEvent): boolean {
  if (/fail|error|check/i.test(event.type)) return true
  if (!isRecord(event.payload)) return false
  return event.payload.error !== undefined || event.payload.stderr !== undefined || event.payload.outcome === 'failed' || event.payload.status === 'failed'
}

function EventEvidence({ event }: { event: AuditEvent }) {
  const summary = eventSummary(event.payload)
  if (!hasFailureEvidence(event) || !isRecord(event.payload)) return <span className="wb-event-summary">{event.task_id ? `${event.task_id} · ` : ''}{summary}</span>
  let payloadText = '证据无法序列化'
  try { payloadText = JSON.stringify(event.payload, null, 2) } catch { /* keep scrubbed fallback */ }
  return <details className="wb-event-evidence"><summary className="wb-event-summary">{event.task_id ? `${event.task_id} · ` : ''}{summary}（展开失败证据）</summary><pre>{payloadText}</pre></details>
}

function actionError(error: unknown): string {
  if (error instanceof WorkspaceApiError && error.status === 409) {
    if (/版本|revision|刷新|重新读取/.test(error.detail)) return `当前版本已变化：${error.detail} 请重新读取运行后再操作。`
    return `当前状态不允许这项操作：${error.detail}`
  }
  if (error instanceof WorkspaceApiError && error.status === 403) return `没有执行这项操作的权限，或会话验证已失效：${error.detail}`
  return errorText(error)
}

function costLabel(value: unknown, missingLabel: string): string {
  if (typeof value === 'number' && Number.isFinite(value)) return `$${value.toFixed(4)}`
  return missingLabel
}

function CheckEvidence({ check, index, context }: { check: Record<string, unknown>; index: number; context?: string }) {
  const passed = checkPassed(check)
  const baseName = typeof check.name === 'string' ? check.name : `检查 ${index + 1}`
  const name = context ? `${context} · ${baseName}` : baseName
  const outcome = passed ? '通过' : check.timeout ? '超时' : check.cancelled ? '已取消' : '未通过'
  const hasOutput = typeof check.stdout === 'string' || typeof check.stderr === 'string'
  return <details className="wb-check-detail"><summary><span>{name}</span><strong className={passed ? 'wb-check-pass' : 'wb-check-fail'}>{outcome}</strong></summary><div className="wb-check-meta"><span>退出码：{check.exit === null || check.exit === undefined ? '—' : String(check.exit)}</span><span>超时：{check.timeout === true ? '是' : '否'}</span>{typeof check.duration_s === 'number' && <span>耗时：{check.duration_s}s</span>}</div>{hasOutput && <div className="wb-check-output">{typeof check.stdout === 'string' && <div><span className="wb-artifact-label">stdout</span><pre>{check.stdout}</pre></div>}{typeof check.stderr === 'string' && <div><span className="wb-artifact-label">stderr</span><pre>{check.stderr}</pre></div>}</div>}</details>
}

interface TaskAttemptGroup {
  id: string
  title: string
  status?: string
  attempts: Record<string, unknown>[]
}

function attemptGroups(run: Run): TaskAttemptGroup[] {
  const titles = new Map<string, string>()
  for (const item of run.plan?.tasks ?? []) titles.set(item.id, item.title)
  const groups = new Map<string, TaskAttemptGroup>()
  for (const source of [run.tasks, run.artifacts?.tasks]) {
    if (!Array.isArray(source)) continue
    for (const raw of source) {
      if (!isRecord(raw) || !Array.isArray(raw.attempts)) continue
      const attempts = raw.attempts.filter(isRecord)
      if (!attempts.length) continue
      const id = typeof raw.id === 'string' ? raw.id : `任务 ${groups.size + 1}`
      if (groups.has(id)) continue
      groups.set(id, {
        id,
        title: typeof raw.title === 'string' ? raw.title : titles.get(id) ?? id,
        status: typeof raw.status === 'string' ? raw.status : undefined,
        attempts,
      })
    }
  }
  return [...groups.values()]
}

function attemptState(attempt: Record<string, unknown>): { text: string; tone: string } {
  const status = attempt.status
  if (status === 'verified' || status === 'completed') return { text: '已验证', tone: 'is-success' }
  if (status === 'cancelled') return { text: '已取消', tone: 'is-muted' }
  if (status === 'failed') return { text: '失败', tone: 'is-failed' }
  return { text: '执行中', tone: 'is-active' }
}

function attemptEvidence(attempt: Record<string, unknown>): string | null {
  const evidence: Record<string, unknown> = {}
  if (typeof attempt.error === 'string' && attempt.error) evidence.error = attempt.error
  if (Array.isArray(attempt.checks) && attempt.checks.length) evidence.checks = attempt.checks
  if (typeof attempt.failure_kind === 'string') evidence.failure_kind = attempt.failure_kind
  if (typeof attempt.retryable === 'boolean') evidence.retryable = attempt.retryable
  return Object.keys(evidence).length ? JSON.stringify(evidence, null, 2) : null
}

function ExecutionAttempts({ run }: { run: Run }) {
  const groups = attemptGroups(run)
  const active = ['queued', 'running', 'verifying'].includes(run.status)
  if (!groups.length) return <section className="wb-execution-card"><div><span className="wb-eyebrow">执行分工与修复</span><h2>模型调用记录</h2><p className="wb-runtime-note">{active ? '正在执行，尚未收到可持久化的模型尝试记录。' : '本次运行尚未记录模型尝试。'}</p></div></section>
  return <section className="wb-execution-card" aria-labelledby="execution-attempts-title"><div className="wb-execution-heading"><div><span className="wb-eyebrow">执行分工与修复</span><h2 id="execution-attempts-title">实际模型尝试</h2><p>每次调用和验证结果均来自本次运行的已记录证据。</p></div><span className="wb-execution-total">{groups.reduce((total, group) => total + group.attempts.length, 0)} 次尝试</span></div><div className="wb-attempt-groups">{groups.map((group) => <article className="wb-attempt-group" key={group.id}><header><div><span className="wb-task-id">{group.id}</span><strong>{group.title}</strong></div><span>{group.attempts.length} 次尝试{group.status ? ` · ${statusLabel(group.status)}` : ''}</span></header><div className="wb-attempt-list">{group.attempts.map((attempt, index) => { const state = attemptState(attempt); const evidence = attemptEvidence(attempt); const profile = typeof attempt.profile === 'string' ? attempt.profile : '未记录角色'; const provider = typeof attempt.provider === 'string' ? attempt.provider : '未记录服务'; const model = typeof attempt.model === 'string' ? attempt.model : '未记录模型'; const reason = typeof attempt.reason === 'string' ? attempt.reason : '未记录选择原因'; const number = typeof attempt.attempt === 'number' ? attempt.attempt : index + 1; return <article className="wb-attempt" key={`${group.id}-${number}-${index}`}><div className="wb-attempt-summary"><div><strong>第 {number} 次</strong><span className={`wb-attempt-state ${state.tone}`}>{state.text}</span></div><span>{profile} · {provider} / {model}</span><span>{reason}</span><strong>{costLabel(attempt.cost_usd, '费用未知')}</strong></div>{evidence && <details className="wb-attempt-evidence"><summary>展开错误与检查证据</summary><pre>{evidence}</pre></details>}</article> })}</div></article>)}</div></section>
}

function FrozenRuntime({ run }: { run: Run }) {
  const snapshot = (run as RunWithFrozenRuntime).runtime_configuration
  if (!snapshot) return <section className="wb-detail-card"><h2>本次冻结配置</h2><p className="wb-runtime-note">服务端没有返回本次运行的配置快照。不会用当前运行配置冒充历史任务配置。</p></section>
  const revision = snapshot.revision ?? snapshot.configuration_revision
  return <section className="wb-detail-card"><details><summary><strong>本次冻结配置</strong></summary><p className="wb-runtime-note">这份配置在本次运行规划时冻结；之后修改运行设置只影响新规划的运行。</p><div className="wb-side-stat"><span>配置修订</span><span>{revision ?? '—'}</span></div><div className="wb-artifact-list">{runtimeRoles.map((role) => { const profile = snapshot.profiles?.[role]; return <div className="wb-artifact" key={role}><span className="wb-artifact-label">{role === 'planner' ? '规划' : role === 'cheap' ? '低成本' : role === 'standard' ? '标准' : '强能力'}</span><span>{profile?.provider || '—'} · {profile?.model || '—'}</span></div> })}</div>{snapshot.limits && <div className="wb-detail-meta"><span>超时：<strong>{snapshot.limits.timeout_s ?? '—'} 秒</strong></span><span>并行：<strong>{snapshot.limits.max_parallel ?? '—'}</strong></span><span>任务上限：<strong>{snapshot.limits.max_tasks ?? '—'}</strong></span><span>未知费用：<strong>{snapshot.limits.unknown_cost_policy === 'allow_bounded' ? '允许有界继续（非美元预算上限）' : '停止并等待确认'}</strong></span></div>}</details></section>
}

function PolicySnapshot({ run, isAdmin }: { run: Run; isAdmin: boolean }) {
  const policy = frozenPolicy(run)
  const automationHref = `/projects/${encodeURIComponent(String(run.project_id))}?tab=automation#project-autonomy`
  if (!policy) return <section className="wb-detail-card"><h2>本次策略</h2><p className="wb-runtime-note">服务端没有返回本次运行的策略冻结快照；不会用当前项目设置冒充历史策略。</p></section>
  return <section className="wb-detail-card"><h2>本次冻结策略</h2><div className="wb-policy-snapshot"><strong>{policy.mode === 'autonomous' ? '自动模式' : '监督模式'} · 策略 v{policy.revision}</strong><p>{policy.mode === 'autonomous' ? '符合冻结范围的计划由后端自动授权；超出范围时会保留实际判断依据。' : '可到项目自动设置复核并接续等待计划；已执行的计划版本和证据会保留。'}</p>{isAdmin && <Link className="wb-text-link" to={automationHref}>复核项目自动设置 →</Link>}</div></section>
}

function PolicyDecisionPanel({ run, isAdmin, canManualApprove, busy, onApprove }: { run: Run; isAdmin: boolean; canManualApprove: boolean; busy: boolean; onApprove: () => void }) {
  const policy = frozenPolicy(run)
  if (run.status !== 'awaiting_approval') return null
  const automationHref = `/projects/${encodeURIComponent(String(run.project_id))}?tab=automation#project-autonomy`
  const manualApproval = canManualApprove ? <section className="wb-policy-manual-approval"><h3>开始下一步</h3><p>仅授权当前计划版本，不会修改项目自动策略。</p><button className="wb-button" disabled={busy} onClick={onApprove}>{busy ? '批准中…' : `确认并开始执行（计划 v${run.revision}）`}</button></section> : null
  if (policy?.mode === 'autonomous') {
    const reasons = (run.triage?.reasons ?? []).filter((reason) => reason.trim())
    return <section className="wb-policy-snapshot"><strong>自动策略没有授权当前计划</strong>{reasons.length ? <><p>判断依据：</p><ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></> : <p>服务端未记录具体判断依据；请查看计划和审计记录后再修改需求或策略。</p>}{isAdmin ? <Link className="wb-text-link" to={automationHref}>复核项目自动设置 →</Link> : <p>请联系管理员调整项目自动策略，或补充要求后生成下一版计划。</p>}{manualApproval}</section>
  }
  return <section className="wb-policy-snapshot"><strong>监督模式等待批准</strong><p>{policy ? `本次运行冻结为监督模式 v${policy.revision}。可在项目自动设置复核并接续，或一次性批准当前版本。` : '本次运行没有返回策略冻结快照；可一次性批准当前计划后执行。'}</p>{isAdmin && <Link className="wb-text-link" to={automationHref}>复核项目自动设置 →</Link>}{manualApproval}{!canManualApprove && <p>当前账号不能批准这个运行，请联系有执行权限的成员。</p>}</section>
}

function DeliveryEvidence({ run }: { run: Run }) {
  const artifacts = run.artifacts
  const checks = Array.isArray(artifacts?.checks) ? artifacts.checks : []
  const checkItems = checks.filter(isRecord)
  const commit = artifacts?.commit
  const baseSha = artifacts?.base_sha
  const prUrl = artifacts?.pr_url
  const totalKnownCost = artifacts?.total_known_cost_usd
  const plannerCost = artifacts?.planner_cost_usd
  const billingIncomplete = artifacts?.billing_incomplete
  const billingMessage = typeof billingIncomplete === 'string' ? billingIncomplete : '部分 provider 费用未知，因此不能保证美元预算。系统不会自动发布，但你仍可在复核后手动发布。'
  const hasBillingIncomplete = billingIncomplete !== undefined && billingIncomplete !== null && billingIncomplete !== false && billingIncomplete !== ''
  return (
    <section className="wb-detail-card" aria-labelledby="delivery-title">
      <h2 id="delivery-title">交付证据</h2>
      <div className="wb-artifact-list">
        <div className="wb-artifact"><span className="wb-artifact-label">基线 SHA</span><span className="wb-sha">{shortSha(baseSha)}</span></div>
        <div className="wb-artifact"><span className="wb-artifact-label">交付 commit SHA</span><span className="wb-sha">{shortSha(commit)}</span></div>
        <div className="wb-artifact"><span className="wb-artifact-label">来源 PR</span>{safeGithubPr(prUrl) ? <a href={prUrl} target="_blank" rel="noreferrer">在 GitHub 打开 PR ↗</a> : <span>{typeof prUrl === 'string' && prUrl ? '已记录链接，但不是受支持的 GitHub PR 地址' : '尚未生成 PR'}</span>}</div>
        <div className="wb-artifact"><span className="wb-artifact-label">费用记录</span><div className="wb-cost-list"><div><span>已知费用（规划与全部尝试）</span><strong>{costLabel(totalKnownCost, '未完整汇总')}</strong></div><div><span>其中规划费用</span><strong>{costLabel(plannerCost, '未报告')}</strong></div><div><span>费用总额</span><strong>{hasBillingIncomplete || typeof totalKnownCost !== 'number' || !Number.isFinite(totalKnownCost) ? '未知' : costLabel(totalKnownCost, '未知')}</strong></div></div></div>
        {hasBillingIncomplete && <div className="wb-billing-warning"><strong>费用记录不完整</strong><span>{billingMessage}</span></div>}
        {checkItems.length > 0 && <div className="wb-artifact"><span className="wb-artifact-label">检查结果</span><div className="wb-checks">{checkItems.map((check, index) => <CheckEvidence check={check} index={index} key={`${String(check.name ?? 'check')}-${index}`} />)}</div></div>}
      </div>
    </section>
  )
}

function PlanPanel({ run, showTasks = true }: { run: Run; showTasks?: boolean }) {
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const statuses = useMemo(() => taskStatusMap(run.tasks), [run.tasks])
  const tasks = useMemo<PlanTask[]>(() => (run.plan?.tasks ?? []).map((task) => ({ ...task, status: statuses.get(task.id) ?? task.status })), [run.plan?.tasks, statuses])
  if (!run.plan) return <EmptyState title="计划尚未返回" description={`当前状态：${run.status === 'planning' ? '规划中' : '暂无计划'}。页面会在运行更新后重新读取。`} />
  return <section className="wb-detail-card" aria-labelledby="plan-title"><div className="wb-detail-kicker">计划版本 {run.revision}</div><h2 id="plan-title">{run.plan.title}</h2><p>{run.plan.summary}</p>{showTasks && <><div className="wb-task-view-switch" role="group" aria-label="任务视图"><button className={`wb-task-button ${view === 'graph' ? 'wb-task-button-active' : ''}`} onClick={() => setView('graph')}>依赖图</button><button className={`wb-task-button ${view === 'list' ? 'wb-task-button-active' : ''}`} onClick={() => setView('list')}>任务列表</button></div>{view === 'graph' ? <div className="wb-task-graph"><TaskGraph tasks={tasks} /></div> : tasks.length ? <div className="wb-task-list">{tasks.map((task) => <article className="wb-task-row" key={task.id}><span className="wb-task-id">{task.id}</span><div><strong>{task.title}</strong><p>{task.prompt}</p>{task.paths.length > 0 && <div className="wb-task-deps">范围：{task.paths.map((path) => <code key={path}>{path}</code>)}</div>}{task.acceptance.length > 0 && <div className="wb-task-deps">验收：{task.acceptance.join('；')}</div>}{task.depends_on.length > 0 && <div className="wb-task-deps">依赖：{task.depends_on.join('、')}</div>}</div><StatusBadge status={task.status ?? 'pending'} /></article>)}</div> : <div className="wb-empty-inline">当前计划没有任务。</div>}</>}</section>
}

function verificationChecks(run: Run): Array<{ check: Record<string, unknown>; context: string }> {
  const evidence: Array<{ check: Record<string, unknown>; context: string }> = []
  if (Array.isArray(run.artifacts?.checks)) for (const check of run.artifacts.checks) if (isRecord(check)) evidence.push({ check, context: '集成检查' })
  for (const source of [run.tasks, run.artifacts?.tasks]) {
    if (!Array.isArray(source)) continue
    for (const task of source) {
      if (!isRecord(task)) continue
      const taskLabel = typeof task.id === 'string' ? `任务 ${task.id}` : '任务检查'
      if (Array.isArray(task.checks)) for (const check of task.checks) if (isRecord(check)) evidence.push({ check, context: taskLabel })
      if (!Array.isArray(task.attempts)) continue
      task.attempts.forEach((attempt, attemptIndex) => {
        if (!isRecord(attempt) || !Array.isArray(attempt.checks)) return
        const number = typeof attempt.attempt === 'number' ? attempt.attempt : attemptIndex + 1
        for (const check of attempt.checks) if (isRecord(check)) evidence.push({ check, context: `${taskLabel} · 第 ${number} 次尝试` })
      })
    }
  }
  return evidence
}

function VerificationEvidence({ run }: { run: Run }) {
  const checks = verificationChecks(run)
  const failure = runGuidance(run).rawEvidence
  if (!checks.length && !failure) return <section className="wb-detail-card"><h2>检查证据</h2><p className="wb-runtime-note">{run.status === 'verifying' ? '正在运行检查，完成后结果会显示在这里。' : '尚未记录可展示的检查结果。'}</p></section>
  return <section className="wb-detail-card" aria-labelledby="verification-title"><h2 id="verification-title">检查证据</h2>{failure && <details className="wb-check-detail"><summary><span>运行记录的失败原因</span><strong className="wb-check-fail">展开证据</strong></summary><pre>{failure}</pre></details>}{checks.length > 0 && <div className="wb-checks">{checks.map(({ check, context }, index) => <CheckEvidence check={check} context={context} index={index} key={`${context}-${String(check.name ?? 'check')}-${index}`} />)}</div>}</section>
}

function ConversationPanel({ messages, loading, error }: { messages: ConversationMessage[] | null; loading: boolean; error: string | null }) {
  if (loading && messages === null) return <div className="wb-loading" role="status">正在读取真实对话记录…</div>
  if (error) return <ErrorNotice message={error} />
  if (!messages?.length) return <EmptyState title="没有对话记录" description="服务端尚未保存可展示的用户与系统消息。" />
  return <div className="wb-conversation">{messages.map((message) => <article className={`wb-message ${message.role === 'user' ? 'wb-message-user' : ''}`} key={String(message.id)}><div className="wb-message-meta"><span>{message.role === 'user' ? '用户' : '系统'}</span><span>{formatDate(message.at)}</span></div><div className="wb-message-content">{message.content}</div></article>)}</div>
}

function EventsPanel({ events, loading, error }: { events: AuditEvent[] | null; loading: boolean; error: string | null }) {
  if (loading && events === null) return <div className="wb-loading" role="status">正在同步审计事件…</div>
  if (error) return <ErrorNotice message={error} />
  if (!events?.length) return <EmptyState title="没有审计事件" description="运行尚未产生可展示的持久化事件。" />
  return <div className="wb-events">{events.map((event) => <article className="wb-event" key={event.id}><span className="wb-event-type">{eventTypeLabel(event.type)}</span><span className="wb-event-time">{formatDate(event.at)}</span><EventEvidence event={event} /></article>)}</div>
}

export default function RunPage({ csrfToken, onUnauthorized, user }: RunPageProps) {
  const isAdmin = user?.role !== 'member'
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const [run, setRun] = useState<Run | null>(null)
  const [tab, setTab] = useState<'conversation' | 'events' | null>(null)
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null)
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [auxError, setAuxError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'clarify' | 'continue' | 'approve' | 'cancel' | 'discard' | 'publish' | 'retry' | 'distill' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [answer, setAnswer] = useState('')
  const [distillName, setDistillName] = useState('')
  const [distillNotice, setDistillNotice] = useState<string | null>(null)
  const eventCursor = useRef(0)
  const runRequestSequence = useRef(0)
  const auxiliarySequence = useRef(0)
  const conversationBusy = useRef(false)
  const eventsBusy = useRef(false)

  useEffect(() => {
    if (location.hash !== '#run-recovery' || !run) return
    document.getElementById('run-recovery')?.scrollIntoView({ block: 'start' })
    document.getElementById('run-recovery-answer')?.focus({ preventScroll: true })
  }, [location.key, run?.id, run?.status])

  const loadRun = useCallback(async (signal?: AbortSignal) => {
    if (!runId) return
    const sequence = runRequestSequence.current + 1
    runRequestSequence.current = sequence
    setLoading(true)
    try { const next = await request<Run>(`/api/v2/runs/${encodeURIComponent(runId)}`, { onUnauthorized, signal }); if (!signal?.aborted && sequence === runRequestSequence.current) setRun(next) } catch (cause) { if (!isAbort(cause) && !signal?.aborted && sequence === runRequestSequence.current && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { if (!signal?.aborted && sequence === runRequestSequence.current) setLoading(false) }
  }, [onUnauthorized, runId])

  useEffect(() => { const controller = new AbortController(); void loadRun(controller.signal); return () => controller.abort() }, [loadRun])
  useEffect(() => { const timer = window.setInterval(() => { void loadRun() }, 5000); return () => window.clearInterval(timer) }, [loadRun])

  const loadConversation = useCallback(async (signal: AbortSignal, sequence: number) => {
    if (!runId) return
    if (conversationBusy.current) return
    conversationBusy.current = true
    try { const result = await request<{ messages: ConversationMessage[] }>(`/api/v2/runs/${encodeURIComponent(runId)}/conversation`, { onUnauthorized, signal }); if (!signal.aborted && sequence === auxiliarySequence.current) setMessages(result.messages) } catch (cause) { if (!isAbort(cause) && !signal.aborted && sequence === auxiliarySequence.current && !(cause instanceof WorkspaceApiError && cause.status === 401)) setAuxError(errorText(cause)) } finally { conversationBusy.current = false }
  }, [onUnauthorized, runId])

  const loadEvents = useCallback(async (signal: AbortSignal, sequence: number) => {
    if (!runId) return
    if (eventsBusy.current) return
    eventsBusy.current = true
    const cursorAtRequest = eventCursor.current
    try { const result = await request<{ events: AuditEvent[]; cursor: number }>(`/api/v2/runs/${encodeURIComponent(runId)}/events?after=${cursorAtRequest}`, { onUnauthorized, signal }); if (!signal.aborted && sequence === auxiliarySequence.current) { setEvents((current) => [...(current ?? []), ...result.events.filter((event) => !(current ?? []).some((existing) => existing.id === event.id))]); eventCursor.current = Math.max(eventCursor.current, result.cursor) } } catch (cause) { if (!isAbort(cause) && !signal.aborted && sequence === auxiliarySequence.current && !(cause instanceof WorkspaceApiError && cause.status === 401)) setAuxError(errorText(cause)) } finally { eventsBusy.current = false }
  }, [onUnauthorized, runId])

  useEffect(() => {
    const sequence = auxiliarySequence.current + 1
    auxiliarySequence.current = sequence
    if (tab === null) return
    setAuxError(null)
    eventCursor.current = tab === 'events' ? 0 : eventCursor.current
    if (tab === 'conversation') setMessages(null)
    else setEvents(null)
    const controller = new AbortController()
    const load = () => { if (tab === 'conversation') void loadConversation(controller.signal, sequence); else void loadEvents(controller.signal, sequence) }
    load()
    const timer = window.setInterval(load, 3500)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [loadConversation, loadEvents, tab, runId])

  useEffect(() => subscribeDataRefresh(() => { void loadRun() }), [loadRun])

  const act = async (kind: 'clarify' | 'continue' | 'approve' | 'cancel' | 'discard' | 'publish') => {
    if (!run || !runId) return
    setBusy(kind); setError(null)
    const body = kind === 'continue' ? { answer: answer.trim(), revision: run.revision, resume_count: (run as Run & { resume_count?: number }).resume_count ?? 0 } : kind === 'clarify' ? { answer: answer.trim() } : kind === 'approve' ? { revision: run.revision } : undefined
    try { const next = await request<Run>(`/api/v2/runs/${encodeURIComponent(runId)}/${kind}`, { method: 'POST', csrfToken, onUnauthorized, body }); setRun(next); notifyDataChanged(); if (kind === 'clarify' || kind === 'continue') setAnswer(''); if (kind === 'approve' || kind === 'continue') navigate(`/runs/${encodeURIComponent(runId)}?view=execution`, { replace: true }) } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(actionError(cause)) } finally { setBusy(null) }
  }

  const retry = async () => {
    if (!run || !runId) return
    setBusy('retry'); setError(null)
    try { const next = await request<Run>(`/api/v3/runs/${encodeURIComponent(runId)}/retry`, { method: 'POST', csrfToken, onUnauthorized }); navigate(`/runs/${encodeURIComponent(String(next.id))}`) } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(actionError(cause)) } finally { setBusy(null) }
  }

  const distill = async (event: FormEvent) => {
    event.preventDefault(); if (!runId || !distillName.trim()) return
    setBusy('distill'); setError(null); setDistillNotice(null)
    try { const candidate = await request<{ id: string; name: string }>('/api/v3/runs/' + encodeURIComponent(runId) + '/distill', { method: 'POST', csrfToken, onUnauthorized, body: { name: distillName.trim() } }); setDistillNotice(`已生成草稿能力“${candidate.name}”（${candidate.id}），请在能力库审阅后配置。`); setDistillName('') } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(null) }
  }

  if (!runId) return <div className="wb-detail-page wb-run-page"><EmptyState title="缺少运行编号" description="请从运行列表打开一个有效的运行。" /></div>
  if (loading && !run) return <div className="wb-detail-page wb-run-page"><div className="wb-loading" role="status">正在读取运行…</div></div>
  if (!run) return <div className="wb-detail-page wb-run-page">{error ? <ErrorNotice message={error} /> : <EmptyState title="找不到这个运行" description="运行可能已被删除，或当前账号无权访问。" action={<Link className="wb-button" to="/">返回工作台</Link>} />}</div>

  const questions = Array.from(new Set([...(run.plan?.questions ?? []), ...(run.triage?.questions ?? [])]))
  const runSource = (run as Run & { source?: { actor_id?: string | number } }).source
  const actorId = runSource?.actor_id
  const canActOnRun = isAdmin || (actorId !== undefined && actorId !== null && String(actorId) === String(user?.id))
  const canCancel = canActOnRun && ACTIVE_STATUSES.includes(run.status)
  const canDiscard = canActOnRun && ['needs_clarification', 'awaiting_approval', 'needs_human', 'failed', 'cancelled'].includes(run.status)
  const canPublish = isAdmin && run.status === 'ready_for_review'
  const canRetry = canActOnRun && ['failed', 'needs_human', 'cancelled'].includes(run.status)
  const canDistill = isAdmin && ['ready_for_review', 'published'].includes(run.status)
  const guidance = runGuidance(run)
  const activeView = runView(searchParams.get('view'), guidance.view)
  const viewHref = (view: RunView) => `/runs/${encodeURIComponent(String(run.id))}?view=${view}`
  const projectHref = `/projects/${encodeURIComponent(String(run.project_id))}?stage=${guidance.stage}`
  const canManualApprove = canActOnRun && run.status === 'awaiting_approval' && questions.length === 0
  const canClarify = canGenerateNextPlan(run, canActOnRun)
  const canContinue = canActOnRun && run.status === 'needs_human' && Boolean(run.plan && run.artifacts?.base_sha && Array.isArray(run.artifacts?.tasks) && run.artifacts.tasks.length)
  return <div className="wb-detail-page wb-run-page"><PageHeader title={run.plan?.title || run.request.slice(0, 80) || `运行 #${run.id}`} description={`需求运行 · 更新于 ${formatDate(run.updated_at)}`} actions={<div className="wb-detail-actions"><button className="wb-button" onClick={() => navigate('/runs')}>返回看板</button><details className="wb-export-menu"><summary className="wb-button">导出记录</summary><div><a href={`/api/v3/runs/${encodeURIComponent(runId)}/export?format=json`}>JSON</a><a href={`/api/v3/runs/${encodeURIComponent(runId)}/export?format=markdown`}>Markdown</a><a href={`/api/v3/runs/${encodeURIComponent(runId)}/export?format=zip`}>ZIP</a></div></details>{canClarify && <Link className="wb-button" to={viewHref('requirements')}>补充要求并生成下一版计划</Link>}{canRetry && <Link className="wb-button wb-button-primary" to={`${viewHref('execution')}#run-recovery`}>处理问题并继续</Link>}{canDiscard && <button className="wb-button wb-button-danger" disabled={busy !== null} onClick={() => void act('discard')}>{busy === 'discard' ? '处理中…' : '标记为废弃'}</button>}{canCancel && <button className="wb-button wb-button-danger" disabled={busy !== null} onClick={() => void act('cancel')}>{busy === 'cancel' ? '处理中…' : run.status === 'ready_for_review' ? '放弃本次交付' : '取消运行'}</button>}{['ready_for_review', 'published'].includes(run.status) && <Link className="wb-button wb-button-primary" to={`${viewHref('delivery')}#deliverables-title`}>查看和下载成果</Link>}</div>} />
    {error && <ErrorNotice message={error} />}
    <p className="wb-run-breadcrumb"><Link to={projectHref}>返回项目闭环</Link><span>·</span><Link to="/runs">运行看板</Link></p>
    <StopReason run={run} canConfigure={isAdmin} />
    <RunJourney run={run} />
    <nav className="wb-view-nav" aria-label="运行工作视图">{RUN_VIEWS.map((view) => <Link key={view} to={viewHref(view)} aria-current={activeView === view ? 'page' : undefined}>{({ requirements: '需求与澄清', plan: '计划', execution: '执行', verification: '验证', delivery: '交付' } as Record<RunView, string>)[view]}</Link>)}</nav>
    <div className="wb-detail-grid"><div className="wb-detail-main"><section className="wb-detail-card"><div className="wb-detail-meta"><span>状态：<StatusBadge status={run.status} /></span><span>当前计划版本：<strong>{run.revision}</strong></span><span>创建于：<strong>{formatDate(run.created_at)}</strong></span></div>
      {activeView === 'requirements' && <><div className="wb-detail-kicker">原始需求</div><h2>需求与澄清</h2><p className="wb-message-content">{run.request}</p>{run.triage?.reasons?.length ? <details><summary>规划时的判断（历史记录）</summary><p className="wb-runtime-note">{run.triage.reasons.join('；')}</p></details> : null}{questions.length > 0 && <><h3>需要补充的事实</h3><ul>{questions.map((question) => <li key={question}>{question}</li>)}</ul></>}{canClarify && <div className="wb-field"><label htmlFor="run-clarification">{questions.length ? `补充要求并生成下一版计划（当前 v${run.revision}）` : '补充目标、范围或验收标准并生成下一版计划'}</label><textarea id="run-clarification" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder={questions.length ? '提供事实、边界或选择，提交后会生成下一版计划。' : '当前没有可展示的具体问题；请补充需要系统据以规划的事实。'} /></div>}{canClarify && <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={!answer.trim() || busy !== null} onClick={() => void act('clarify')}>{busy === 'clarify' ? '正在生成下一版计划…' : '补充要求并生成下一版计划'}</button></div>}{!canClarify && guidance.kind !== 'requirements' && <p className="wb-runtime-note">当前无需补充需求。请根据上方状态指引进入对应工作视图。</p>}</>}
      {activeView === 'plan' && <><PolicyDecisionPanel run={run} isAdmin={isAdmin} canManualApprove={canManualApprove} busy={busy !== null} onApprove={() => void act('approve')} /><PlanPanel run={run} /><PlanVersions run={run} onUnauthorized={onUnauthorized} /></>}
      {activeView === 'execution' && <><ExecutionAttempts run={run} />{['needs_human', 'failed', 'cancelled'].includes(run.status) && <section className="wb-policy-snapshot" id="run-recovery" aria-labelledby="run-recovery-title"><h2 id="run-recovery-title">处理问题，继续任务</h2>{executionFailure(run)?.startsWith('out-of-scope changes:') ? <><p>这次修改涉及当前任务范围以外的文件。回答后，助手会按原任务范围修正草稿并继续；如果确实要改变任务范围，可另行修改计划。</p><p>涉及文件：{executionFailure(run)?.slice('out-of-scope changes:'.length)}</p></> : executionFailure(run)?.startsWith('worker changed test/check infrastructure:') ? <p>这次改动包含测试配置。是否保留正常的项目依赖信息，并恢复原有测试配置后继续？你可以直接回答，助手会在当前草稿上处理。</p> : <p>回答当前问题，助手会保留原计划和已完成成果，从暂停的步骤继续。</p>}{canContinue ? <form onSubmit={(event) => { event.preventDefault(); if (answer.trim() && busy === null) void act('continue') }}><div className="wb-field"><label htmlFor="run-recovery-answer">你的回答</label><textarea id="run-recovery-answer" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="例如：保留项目依赖，恢复原有测试配置，然后继续完成。" /></div><p>回答和失败证据会直接交给执行助手，不重新规划。已完成的任务不会重新调用模型；未完成的草稿会继续修正并验证，调用费用计入本次任务。</p><button className="wb-button wb-button-primary" disabled={!answer.trim() || busy !== null}>{busy === 'continue' ? '正在继续…' : '回答并继续执行'}</button>{error && <ErrorNotice message={error} />}</form> : canActOnRun ? <p>当前没有可直接恢复的执行现场，请通过“补充要求并生成下一版计划”或下方新运行入口处理。</p> : <p>只有任务发起人或管理员可以处理这次运行，请联系他们继续。</p>}{canRetry && <details><summary>按当前配置创建新运行</summary><p>保留本次记录，使用当前项目配置重新规划。职能体任务沿用原职能体版本与模型快照。相同问题未处理时可能再次失败，并产生新的调用费用。</p><button className="wb-button" disabled={busy !== null} onClick={() => void retry()}>{busy === 'retry' ? '正在创建…' : '创建新运行'}</button></details>}{isAdmin && <p><Link to={`/projects/${encodeURIComponent(String(run.project_id))}?tab=settings#project-budget`}>调整项目预算</Link> · <Link to="/settings/runtime">查看运行配置</Link></p>}</section>}<PlanPanel run={run} /></> }
      {activeView === 'verification' && <VerificationEvidence run={run} />}
      {activeView === 'delivery' && <><Deliverables run={run} csrfToken={csrfToken} onUnauthorized={onUnauthorized} isAdmin={isAdmin} onPublish={canPublish ? () => void act('publish') : undefined} publishing={busy === 'publish'} /><DeliveryEvidence run={run} />{canDistill && <section className="wb-detail-card wb-distill-card"><div><span className="wb-eyebrow">能力生产</span><h2>能力归档</h2><p>交付完成后系统会自动留下可追溯的能力草稿；你也可手动生成一个额外草稿供比较或补充。</p></div>{(() => { const candidateId = (run as Run & { capability_candidate_id?: unknown }).capability_candidate_id ?? run.artifacts?.capability_candidate_id; return typeof candidateId === 'string' && candidateId ? <div className="wb-notice">本次交付已归档为能力草稿。<Link className="wb-text-link" to={`/capabilities?selected=${encodeURIComponent(candidateId)}`}>打开已归档能力 →</Link></div> : null })()}<details className="wb-distill-manual"><summary>手动生成额外草稿</summary><form onSubmit={distill} className="wb-distill-form"><input required maxLength={120} value={distillName} onChange={(event) => setDistillName(event.target.value)} placeholder="候选能力名称" /><button className="wb-button wb-button-primary" disabled={busy !== null}>{busy === 'distill' ? '提炼中…' : '生成额外草稿'}</button></form></details>{distillNotice && <div className="wb-notice">{distillNotice} <Link className="wb-text-link" to="/capabilities">打开能力库 →</Link></div>}</section>}</>}
    </section><details className="wb-detail-card"><summary><strong>对话和审计记录</strong></summary><div className="wb-detail-tabs" role="tablist" aria-label="运行记录"><button className={`wb-tab ${tab === 'conversation' ? 'wb-tab-active' : ''}`} role="tab" aria-selected={tab === 'conversation'} onClick={() => setTab('conversation')}>对话</button><button className={`wb-tab ${tab === 'events' ? 'wb-tab-active' : ''}`} role="tab" aria-selected={tab === 'events'} onClick={() => setTab('events')}>审计事件</button></div>{tab === null ? <p className="wb-runtime-note">按需打开对话或审计事件，避免在不查看时重复读取记录。</p> : tab === 'conversation' ? <ConversationPanel messages={messages} loading={loading} error={auxError} /> : <EventsPanel events={events} loading={loading} error={auxError} />}</details>{isAdmin && <RunKnowledge key={`${run.id}:${run.revision}`} runId={run.id} revision={run.revision} status={run.status} artifacts={run.artifacts} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}</div><aside className="wb-detail-rail" aria-label="运行详情摘要"><section className="wb-detail-card"><h2>运行概览</h2><div className="wb-side-stat"><span>运行 ID</span><span className="wb-side-stat-id" title={String(run.id)}>{run.id}</span></div><div className="wb-side-stat"><span>工程 ID</span><span className="wb-side-stat-id" title={String(run.project_id)}>{run.project_id}</span></div><div className="wb-side-stat"><span>状态</span><span><StatusBadge status={run.status} /></span></div><div className="wb-side-stat"><span>当前指引</span><span>{guidance.label}</span></div><div className="wb-side-stat"><span>风险</span><span>{run.triage?.risk ? { low: '低风险', medium: '中风险', high: '高风险' }[run.triage.risk] : '—'}</span></div></section><PolicySnapshot run={run} isAdmin={isAdmin} /><FrozenRuntime run={run} /></aside></div></div>
}
