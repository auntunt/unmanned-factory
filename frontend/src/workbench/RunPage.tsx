import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import RunKnowledge from '../workspace/RunKnowledge'
import TaskGraph from '../workspace/TaskGraph'
import type { AuditEvent, ConversationMessage, PlanTask, Run, RunStatus, TaskStatus } from '../workspace/types'
import type { RuntimeLimits, RuntimeProfiles } from './runtime-types'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge, errorText, type PageProps } from './ui'
import './detail.css'

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
  if (error instanceof WorkspaceApiError && error.status === 409) return `当前版本已变化：${error.detail} 请重新读取运行后再操作。`
  return errorText(error)
}

function costLabel(value: unknown, missingLabel: string): string {
  if (typeof value === 'number' && Number.isFinite(value)) return `$${value.toFixed(4)}`
  return missingLabel
}

function CheckEvidence({ check, index }: { check: Record<string, unknown>; index: number }) {
  const passed = check.outcome === 'passed' || check.outcome === 'pass' || check.status === 'passed' || check.exit === 0
  const name = typeof check.name === 'string' ? check.name : `检查 ${index + 1}`
  const outcome = passed ? '通过' : check.timeout ? '超时' : check.cancelled ? '已取消' : '未通过'
  const hasOutput = typeof check.stdout === 'string' || typeof check.stderr === 'string'
  return <details className="wb-check-detail"><summary><span>{name}</span><strong className={passed ? 'wb-check-pass' : 'wb-check-fail'}>{outcome}</strong></summary><div className="wb-check-meta"><span>退出码：{check.exit === null || check.exit === undefined ? '—' : String(check.exit)}</span><span>超时：{check.timeout === true ? '是' : '否'}</span>{typeof check.duration_s === 'number' && <span>耗时：{check.duration_s}s</span>}</div>{hasOutput && <div className="wb-check-output">{typeof check.stdout === 'string' && <div><span className="wb-artifact-label">stdout</span><pre>{check.stdout}</pre></div>}{typeof check.stderr === 'string' && <div><span className="wb-artifact-label">stderr</span><pre>{check.stderr}</pre></div>}</div>}</details>
}

function FrozenRuntime({ run }: { run: Run }) {
  const snapshot = (run as RunWithFrozenRuntime).runtime_configuration
  if (!snapshot) return <section className="wb-detail-card"><h2>本次冻结配置</h2><p className="wb-runtime-note">服务端没有返回本次运行的配置快照。不会用当前运行配置冒充历史任务配置。</p></section>
  const revision = snapshot.revision ?? snapshot.configuration_revision
  return <section className="wb-detail-card"><details><summary><strong>本次冻结配置</strong></summary><p className="wb-runtime-note">这份配置在本次运行规划时冻结；之后修改运行设置只影响新规划的运行。</p><div className="wb-side-stat"><span>配置修订</span><span>{revision ?? '—'}</span></div><div className="wb-artifact-list">{runtimeRoles.map((role) => { const profile = snapshot.profiles?.[role]; return <div className="wb-artifact" key={role}><span className="wb-artifact-label">{role === 'planner' ? '规划' : role === 'cheap' ? '低成本' : role === 'standard' ? '标准' : '强能力'}</span><span>{profile?.provider || '—'} · {profile?.model || '—'}</span></div> })}</div>{snapshot.limits && <div className="wb-detail-meta"><span>超时：<strong>{snapshot.limits.timeout_s ?? '—'} 秒</strong></span><span>并行：<strong>{snapshot.limits.max_parallel ?? '—'}</strong></span><span>任务上限：<strong>{snapshot.limits.max_tasks ?? '—'}</strong></span><span>未知费用：<strong>{snapshot.limits.unknown_cost_policy === 'allow_bounded' ? '允许有界继续（非美元预算上限）' : '停止并等待确认'}</strong></span></div>}</details></section>
}

function DeliveryEvidence({ run }: { run: Run }) {
  const artifacts = run.artifacts
  const checks = Array.isArray(artifacts?.checks) ? artifacts.checks : []
  const checkItems = checks.filter(isRecord)
  const commit = artifacts?.commit
  const baseSha = artifacts?.base_sha
  const prUrl = artifacts?.pr_url
  const knownCost = artifacts?.known_cost_usd
  const observedCost = artifacts?.observed_cost_usd
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
        <div className="wb-artifact"><span className="wb-artifact-label">费用记录</span><div className="wb-cost-list"><div><span>已知费用小计</span><strong>{costLabel(knownCost, '未报告')}</strong></div><div><span>费用总额</span><strong>{observedCost === null ? '未知' : costLabel(observedCost, '未报告')}</strong></div></div></div>
        {hasBillingIncomplete && <div className="wb-billing-warning"><strong>费用记录不完整</strong><span>{billingMessage}</span></div>}
        {checkItems.length > 0 && <div className="wb-artifact"><span className="wb-artifact-label">检查结果</span><div className="wb-checks">{checkItems.map((check, index) => <CheckEvidence check={check} index={index} key={`${String(check.name ?? 'check')}-${index}`} />)}</div></div>}
      </div>
    </section>
  )
}

function PlanPanel({ run }: { run: Run }) {
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const statuses = useMemo(() => taskStatusMap(run.tasks), [run.tasks])
  const tasks = useMemo<PlanTask[]>(() => (run.plan?.tasks ?? []).map((task) => ({ ...task, status: statuses.get(task.id) ?? task.status })), [run.plan?.tasks, statuses])
  if (!run.plan) return <EmptyState title="计划尚未返回" description={`当前状态：${run.status === 'planning' ? '规划中' : '暂无计划'}。页面会在运行更新后重新读取。`} />
  return <section className="wb-detail-card" aria-labelledby="plan-title"><div className="wb-detail-kicker">计划版本 {run.revision}</div><h2 id="plan-title">{run.plan.title}</h2><p>{run.plan.summary}</p><div className="wb-task-view-switch" role="group" aria-label="任务视图"><button className={`wb-task-button ${view === 'graph' ? 'wb-task-button-active' : ''}`} onClick={() => setView('graph')}>依赖图</button><button className={`wb-task-button ${view === 'list' ? 'wb-task-button-active' : ''}`} onClick={() => setView('list')}>任务列表</button></div>{view === 'graph' ? <div className="wb-task-graph"><TaskGraph tasks={tasks} /></div> : tasks.length ? <div className="wb-task-list">{tasks.map((task) => <article className="wb-task-row" key={task.id}><span className="wb-task-id">{task.id}</span><div><strong>{task.title}</strong><p>{task.prompt}</p>{task.depends_on.length > 0 && <div className="wb-task-deps">依赖：{task.depends_on.join('、')}</div>}</div><StatusBadge status={task.status ?? 'pending'} /></article>)}</div> : <div className="wb-empty-inline">当前计划没有任务。</div>}</section>
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
  return <div className="wb-events">{events.map((event) => <article className="wb-event" key={event.id}><span className="wb-event-type">{event.type}</span><span className="wb-event-time">{formatDate(event.at)}</span><EventEvidence event={event} /></article>)}</div>
}

export default function RunPage({ csrfToken, onUnauthorized }: RunPageProps) {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [run, setRun] = useState<Run | null>(null)
  const [tab, setTab] = useState<'plan' | 'conversation' | 'events'>('plan')
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null)
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [auxError, setAuxError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'clarify' | 'approve' | 'cancel' | 'publish' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [answer, setAnswer] = useState('')
  const eventCursor = useRef(0)
  const runRequestSequence = useRef(0)
  const auxiliarySequence = useRef(0)
  const conversationBusy = useRef(false)
  const eventsBusy = useRef(false)

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
    if (tab === 'plan') return
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

  const act = async (kind: 'clarify' | 'approve' | 'cancel' | 'publish') => {
    if (!run || !runId) return
    setBusy(kind); setError(null)
    const body = kind === 'clarify' ? { answer: answer.trim() } : kind === 'approve' ? { revision: run.revision } : undefined
    try { const next = await request<Run>(`/api/v2/runs/${encodeURIComponent(runId)}/${kind}`, { method: 'POST', csrfToken, onUnauthorized, body }); setRun(next); if (kind === 'clarify') setAnswer('') } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(actionError(cause)) } finally { setBusy(null) }
  }

  if (!runId) return <main className="wb-detail-page"><EmptyState title="缺少运行编号" description="请从运行列表打开一个有效的运行。" /></main>
  if (loading && !run) return <main className="wb-detail-page"><div className="wb-loading" role="status">正在读取运行…</div></main>
  if (!run) return <main className="wb-detail-page">{error ? <ErrorNotice message={error} /> : <EmptyState title="找不到这个运行" description="运行可能已被删除，或当前账号无权访问。" action={<Link className="wb-button" to="/">返回工作台</Link>} />}</main>

  const questions = Array.from(new Set([...(run.plan?.questions ?? []), ...(run.triage?.questions ?? [])]))
  const canClarify = ['needs_clarification', 'needs_human', 'awaiting_approval'].includes(run.status)
  const canApprove = run.status === 'awaiting_approval' && questions.length === 0
  const canCancel = ACTIVE_STATUSES.includes(run.status)
  const canPublish = run.status === 'ready_for_review'
  return <main className="wb-detail-page"><PageHeader title={run.plan?.title || `运行 #${run.id}`} description={`运行 #${run.id} · 更新时间 ${formatDate(run.updated_at)}`} actions={<div className="wb-detail-actions"><button className="wb-button" onClick={() => navigate('/projects')}>返回项目</button><a className="wb-button" href={`/api/v2/runs/${encodeURIComponent(runId)}/export`} target="_blank" rel="noreferrer">导出工程记录</a>{canCancel && <button className="wb-button wb-button-danger" disabled={busy !== null} onClick={() => void act('cancel')}>{busy === 'cancel' ? '处理中…' : run.status === 'ready_for_review' ? '放弃本次交付' : '取消运行'}</button>}{canPublish && <button className="wb-button wb-button-primary" disabled={busy !== null} onClick={() => void act('publish')}>{busy === 'publish' ? '发布中…' : '发布交付'}</button>}</div>} />
    {error && <ErrorNotice message={error} />}
    <div className="wb-detail-grid"><div className="wb-detail-main"><section className="wb-detail-card"><div className="wb-detail-meta"><span>状态：<StatusBadge status={run.status} /></span><span>当前计划版本：<strong>{run.revision}</strong></span><span>创建于：<strong>{formatDate(run.created_at)}</strong></span></div><div className="wb-detail-tabs" role="tablist" aria-label="运行内容"><button className={`wb-tab ${tab === 'plan' ? 'wb-tab-active' : ''}`} role="tab" aria-selected={tab === 'plan'} onClick={() => setTab('plan')}>计划与任务</button><button className={`wb-tab ${tab === 'conversation' ? 'wb-tab-active' : ''}`} role="tab" aria-selected={tab === 'conversation'} onClick={() => setTab('conversation')}>对话</button><button className={`wb-tab ${tab === 'events' ? 'wb-tab-active' : ''}`} role="tab" aria-selected={tab === 'events'} onClick={() => setTab('events')}>审计事件</button></div>{tab === 'plan' && <PlanPanel run={run} />}{tab === 'conversation' && <ConversationPanel messages={messages} loading={loading} error={auxError} />}{tab === 'events' && <EventsPanel events={events} loading={loading} error={auxError} />}</section>{canClarify && <section className="wb-detail-card wb-question"><h2>{questions.length ? '需要确认' : '补充信息'}</h2>{questions.length > 0 && <ul>{questions.map((question) => <li key={question}>{question}</li>)}</ul>}<div className="wb-field"><label htmlFor="run-clarification">针对当前计划版本 {run.revision} 的补充</label><textarea id="run-clarification" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="提供事实、边界或选择，提交后会重新规划。" /></div><div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={!answer.trim() || busy !== null} onClick={() => void act('clarify')}>{busy === 'clarify' ? '提交并重新规划…' : '提交补充并重新规划'}</button>{canApprove && <button className="wb-button" disabled={busy !== null} onClick={() => void act('approve')}>{busy === 'approve' ? '批准中…' : `批准计划版本 ${run.revision}`}</button>}</div></section>}{canApprove && !canClarify && <section className="wb-detail-card"><h2>批准当前计划</h2><p>计划没有未解决问题。批准后将按当前运行配置进入有界任务调度。</p><div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy !== null} onClick={() => void act('approve')}>{busy === 'approve' ? '批准中…' : `批准计划版本 ${run.revision}`}</button></div></section>}<RunKnowledge key={`${run.id}:${run.revision}`} runId={run.id} revision={run.revision} status={run.status} artifacts={run.artifacts} csrfToken={csrfToken} onUnauthorized={onUnauthorized} /></div><aside className="wb-detail-rail"><section className="wb-detail-card"><h2>运行概览</h2><div className="wb-side-stat"><span>运行 ID</span><span>{run.id}</span></div><div className="wb-side-stat"><span>工程 ID</span><span>{run.project_id}</span></div><div className="wb-side-stat"><span>状态</span><span><StatusBadge status={run.status} /></span></div><div className="wb-side-stat"><span>风险</span><span>{run.triage?.risk ?? '—'}</span></div>{run.triage?.reasons?.length ? <div className="wb-runtime-note">{run.triage.reasons.join('；')}</div> : null}</section><FrozenRuntime run={run} /><DeliveryEvidence run={run} /></aside></div></main>
}
