import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import { EmptyState, ErrorNotice, formatDate, PageHeader, StatusBadge, errorText, type PageProps } from './ui'
import type { EngineeringStage, EngineeringStageItem, EngineeringSummary, OverviewData } from './v3-types'
import './overview.css'
import EngineeringLoop from './EngineeringLoop'

const STAGE_FALLBACK: EngineeringStage[] = [
  { id: 'intake', label: '需求澄清', description: '明确目标、范围和验收方式。', count: 0, unit: '条需求', items: [] },
  { id: 'plan', label: '方案规划', description: '形成可确认、可追溯的执行方案。', count: 0, unit: '项规划', items: [] },
  { id: 'build', label: '开发执行', description: '在项目边界内完成有界任务。', count: 0, unit: '项执行', items: [] },
  { id: 'verify', label: '质量验证', description: '运行检查并保留验证证据。', count: 0, unit: '项验证', items: [] },
  { id: 'deliver', label: '交付发布', description: '把通过验证的结果交付到项目。', count: 0, unit: '项交付', items: [] },
  { id: 'reuse', label: '能力沉淀', description: '把交付经验整理成下一次可复用的能力。', count: 0, unit: '项能力', items: [] },
]

const EVENT_LABELS: Record<string, string> = {
  'run.created': '需求进入', 'plan.created': '方案生成', 'plan.approved': '方案确认',
  'task.started': '执行开始', 'task.completed': '任务完成', 'attempt.failed': '执行异常',
  'run.failed': '运行失败', 'run.recovered': '运行恢复', 'run.verified': '验证完成',
  'github.published': '已发布', 'capability.candidate_created': '能力沉淀',
}
const IMPORTANT_EVENT_TYPES = new Set(Object.keys(EVENT_LABELS))

type LoadState = { value: OverviewData | null; loading: boolean; error: string | null }

function taskItems(run: Run): Array<{ status?: string }> {
  if (Array.isArray(run.tasks)) return run.tasks.filter((item): item is { status?: string } => Boolean(item && typeof item === 'object'))
  return run.plan?.tasks ?? []
}

export function taskGroupCounts(runs: Run[]): { total: number; active: number; completed: number; blocked: number } {
  const items = runs.flatMap(taskItems)
  return { total: items.length, active: items.filter((item) => ['queued', 'running'].includes(item.status ?? '')).length, completed: items.filter((item) => ['completed', 'verified'].includes(item.status ?? '')).length, blocked: items.filter((item) => ['blocked', 'failed', 'cancelled'].includes(item.status ?? '')).length }
}

function emptyEngineering(): EngineeringSummary {
  return { stages: STAGE_FALLBACK, verified_runs: 0, published_runs: 0, distilled_runs: 0, reused_runs: 0, draft_capabilities: 0, ready_capabilities: 0 }
}

function stageData(data: OverviewData): EngineeringSummary {
  if (!data.engineering) return emptyEngineering()
  const byId = new Map(data.engineering.stages.map((stage) => [stage.id, stage]))
  const stages = STAGE_FALLBACK.map((fallback) => byId.get(fallback.id) ?? fallback)
  return { ...data.engineering, stages }
}

function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? '运行进展'
}

function stageAction(stageId: string): { href: string; label: string } {
  if (stageId === 'intake') return { href: '/projects', label: '发起需求' }
  if (stageId === 'reuse') return { href: '/capabilities', label: '查看能力库' }
  return { href: '/runs', label: '打开运行看板' }
}

function StageItem({ item }: { item: EngineeringStageItem }) {
  const capabilityStatus = item.status === 'draft' ? '草稿' : item.status === 'ready' ? '可调用' : null
  return <Link className="ov3-item" to={item.href}>
    <span className="ov3-item-copy"><strong>{item.title}</strong><small>{item.project_name || '项目'} · {formatDate(item.updated_at)}</small>{item.detail && <span>{item.detail}</span>}</span>
    {capabilityStatus ? <span className={`ov3-stage-status ov3-stage-status-${item.status}`}>{capabilityStatus}</span> : <StatusBadge status={item.status} />}
    <span className="ov3-arrow" aria-hidden="true">→</span>
  </Link>
}

export default function OverviewPage({ onUnauthorized }: PageProps) {
  const [state, setState] = useState<LoadState>({ value: null, loading: true, error: null })
  const [selectedStage, setSelectedStage] = useState('')
  const controllerRef = useRef<AbortController | null>(null)

  const load = useCallback((manual = false) => {
    if (!manual && typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    if (!manual && controllerRef.current) return
    if (manual) controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setState((current) => ({ ...current, loading: true, error: null }))
    void request<OverviewData>('/api/v3/overview', { onUnauthorized, signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setState({ value, loading: false, error: null })
    }).catch((cause) => {
      if (!controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setState((current) => ({ ...current, loading: false, error: errorText(cause) }))
    }).finally(() => {
      if (controllerRef.current === controller) { controllerRef.current = null; if (!controller.signal.aborted) setState((current) => ({ ...current, loading: false })) }
    })
  }, [onUnauthorized])

  useEffect(() => {
    load(true)
    const timer = window.setInterval(() => load(), 15000)
    return () => { controllerRef.current?.abort(); window.clearInterval(timer) }
  }, [load])

  const data = state.value
  const engineering = useMemo(() => data ? stageData(data) : emptyEngineering(), [data])
  const activeStage = engineering.stages.find((stage) => stage.id === selectedStage) ?? engineering.stages.find((stage) => stage.count > 0) ?? engineering.stages[0]
  const attention = useMemo(() => (data?.attention ?? []).slice(0, 6), [data])
  const events = useMemo(() => (data?.recent_events ?? []).filter((event) => IMPORTANT_EVENT_TYPES.has(event.type)).slice(0, 4), [data])

  useEffect(() => {
    if (data && activeStage && !selectedStage) setSelectedStage(activeStage.id)
  }, [activeStage, selectedStage, data])

  return <div className="wb-page ov3-page">
    <PageHeader title="工程总览" description="从需求到交付，让每次工作留下可复用能力。" actions={<><button className="wb-button wb-button-secondary ov3-refresh" onClick={() => load(true)} aria-label="刷新工程总览">{state.loading && data ? '更新中…' : '刷新'}</button><Link className="wb-button wb-button-primary" to="/projects">发起需求 <span aria-hidden="true">→</span></Link></>} />
    {state.error && <ErrorNotice message={data ? `刷新失败，继续显示上一份记录：${state.error}` : state.error} />}
    {state.loading && !data && <div className="ov3-card wb-card" role="status" aria-label="正在加载工程总览"><div className="ov3-loading"><span /><span /><span /></div></div>}
    {data && <>
      <section className="ov3-summary" aria-label="运营摘要">
        <div><span>进行中</span><strong>{data.active_runs}</strong><small>正在规划、执行或验证</small></div>
        <div className={data.attention_runs > 0 ? 'is-attention' : ''}><span>需你判断</span><strong>{data.attention_runs}</strong><small>澄清、审批或异常</small></div>
        <div><span>已验证</span><strong>{engineering.verified_runs}</strong><small>通过质量检查的运行</small></div>
        <div><span>已发布</span><strong>{engineering.published_runs}</strong><small>已记录发布结果</small></div>
      </section>

      <section className="ov3-card wb-card ov3-flow-card ov3-loop-workbench" aria-labelledby="ov3-flow-title">
        <div className="ov3-section-head"><div><span className="wb-eyebrow">持续交付 · 经验回流</span><h2 id="ov3-flow-title">软件工程闭环</h2><p>每次交付留下经验，经过验证并启用后，回到下一次需求。</p></div><span className="ov3-flow-note">顺时针推进 · 点击节点查看</span></div>
        <div className="ov3-cycle-layout">
          <EngineeringLoop stages={engineering.stages} selectedId={activeStage?.id ?? 'intake'} onSelect={setSelectedStage} />
          <div className="ov3-cycle-inspector">
            <section id="ov3-stage-details" className="ov3-cycle-records" aria-labelledby="ov3-stage-detail-title">
              <div className="ov3-section-head"><div><span className="wb-eyebrow">对应工作</span><h2 id="ov3-stage-detail-title">{activeStage?.label ?? '阶段记录'}</h2><p>{activeStage?.description}</p></div>{activeStage && <span className="ov3-stage-total">{activeStage.count} {activeStage.unit}</span>}</div>
              {activeStage && activeStage.items.length > 0 ? <div className="ov3-item-list">{activeStage.items.slice(0, 6).map((item) => <StageItem item={item} key={item.id} />)}</div> : <EmptyState title="这个阶段暂时没有记录" description={activeStage?.id === 'intake' ? '从一条清晰需求开始，系统会把后续阶段串起来。' : '真实运行产生阶段记录后，会在这里显示阶段产出。'} action={<Link className="wb-button wb-button-secondary" to={stageAction(activeStage?.id ?? 'intake').href}>{stageAction(activeStage?.id ?? 'intake').label}</Link>} />}
              {activeStage && activeStage.count > activeStage.items.length && <p className="ov3-list-foot">当前显示前 {activeStage.items.length} 条，共 {activeStage.count} {activeStage.unit}。</p>}
            </section>
            <section className="ov3-cycle-attention" aria-labelledby="ov3-attention-title">
              <div className="ov3-section-head"><div><span className="wb-eyebrow">例外处理</span><h2 id="ov3-attention-title">需要你的判断</h2></div><span className="ov3-attention-count">{data.attention_runs}</span></div>
              {attention.length === 0 ? <div className="ov3-cycle-clear"><span aria-hidden="true">✓</span><div><strong>当前无需你介入</strong><p>待澄清问题和异常会在这里集中呈现。</p></div></div> : <><div className="ov3-item-list">{attention.map((item) => <Link className="ov3-attention-row" to={`/runs/${encodeURIComponent(String(item.id))}`} key={item.id}><span className="ov3-attention-mark">!</span><span><strong>{item.title || item.reason || '运行需要处理'}</strong><small>{item.project_name || '项目'} · {formatDate(item.updated_at)}</small></span><StatusBadge status={item.status} /></Link>)}</div>{attention.length < data.attention_runs && <p className="ov3-list-foot">当前显示前 {attention.length} 条，共 {data.attention_runs} 条。</p>}</>}
            </section>
          </div>
        </div>
      </section>

      <section className="ov3-card wb-card ov3-outcomes" aria-labelledby="ov3-outcomes-title">
        <div className="ov3-section-head"><div><span className="wb-eyebrow">经验复用</span><h2 id="ov3-outcomes-title">让下一次交付有据可循</h2></div><Link className="wb-text-link" to="/costs">查看成本与调用记录 →</Link></div>
        <div className="ov3-outcome-grid"><div><strong>{engineering.distilled_runs}</strong><span>来源运行</span><small>交付后留下能力来源的运行。</small></div><div><strong>{engineering.ready_capabilities}</strong><span>可调用能力</span><small>已启用，可绑定版本并调用。</small></div><div><strong>{engineering.reused_runs}</strong><span>能力引用运行</span><small>实际引用了已有交付能力的运行。</small></div><div><strong>${data.known_cost_usd.toFixed(2)}</strong><span>已知成本</span><small>另有 {data.unknown_cost_runs} 次费用未知。</small></div></div>
        <p className="ov3-capability-note">能力库另有 <b>{engineering.draft_capabilities}</b> 项草稿，可继续完善、验证并启用。</p>
      </section>

      <section className="ov3-card wb-card ov3-events" aria-labelledby="ov3-events-title">
        <div className="ov3-section-head"><div><span className="wb-eyebrow">重要动态</span><h2 id="ov3-events-title">最近留下的事实</h2></div><Link className="wb-text-link" to="/runs">查看全部运行 →</Link></div>
        {events.length === 0 ? <EmptyState title="最近记录中没有阶段动态" description="完整执行过程可在运行详情中查看。" action={<Link className="wb-button wb-button-secondary" to="/projects">发起需求</Link>} /> : <div className="ov3-event-list">{events.map((event) => <Link className="ov3-event-row" to={`/runs/${encodeURIComponent(String(event.run_id))}`} key={event.id}><span className="ov3-event-dot" aria-hidden="true" /><span><strong>{event.run_title || '运行进展'}</strong><small>{event.project_name || '项目'} · {eventLabel(event.type)} · {formatDate(event.at)}</small></span><span className="ov3-arrow" aria-hidden="true">→</span></Link>)}</div>}
      </section>
    </>}
  </div>
}
