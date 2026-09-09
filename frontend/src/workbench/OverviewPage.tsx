import { nextRunAction, runGuidance } from './run-guidance'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import type { Run } from '../workspace/types'
import { EmptyState, ErrorNotice, formatDate, PageHeader, errorText, type PageProps } from './ui'
import type { OverviewData } from './v3-types'
import { PROJECT_STAGES, projectStageHref } from './project-stages'
import AttentionList from './AttentionList'
import { subscribeDataRefresh } from './data-refresh'
import './overview.css'
import './project-workspace.css'

const EVENT_LABELS: Record<string, string> = {
  'run.created': '需求进入', 'plan.created': '方案生成', 'plan.approved': '方案确认',
  'task.started': '执行开始', 'task.completed': '任务完成', 'attempt.failed': '执行异常',
  'run.failed': '运行失败', 'run.recovered': '运行恢复', 'run.verified': '验证完成',
  'github.published': '已发布', 'capability.candidate_created': '能力沉淀',
}

export function taskGroupCounts(runs: Run[]) {
  const items = runs.flatMap((run) => Array.isArray(run.tasks)
    ? run.tasks.filter((item): item is { status?: string } => Boolean(item && typeof item === 'object'))
    : run.plan?.tasks ?? [])
  return { total: items.length, active: items.filter((item) => ['queued', 'running'].includes(item.status ?? '')).length,
    completed: items.filter((item) => ['completed', 'verified'].includes(item.status ?? '')).length,
    blocked: items.filter((item) => ['blocked', 'failed', 'cancelled'].includes(item.status ?? '')).length }
}

export default function OverviewPage({ onUnauthorized, user }: PageProps) {
  const [data, setData] = useState<OverviewData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const load = useCallback((manual = false) => {
    if (!manual && (document.visibilityState !== 'visible' || controllerRef.current)) return
    controllerRef.current?.abort()
    const controller = new AbortController(); controllerRef.current = controller
    setLoading(true); setError(null)
    void request<OverviewData>('/api/v3/overview', { onUnauthorized, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setData(value) })
      .catch((cause) => { if (!controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) })
      .finally(() => { if (controllerRef.current === controller) { controllerRef.current = null; if (!controller.signal.aborted) setLoading(false) } })
  }, [onUnauthorized])
  useEffect(() => { load(true); const timer = window.setInterval(() => load(), 5000); return () => { controllerRef.current?.abort(); window.clearInterval(timer) } }, [load])
  useEffect(() => subscribeDataRefresh(() => load(true)), [load])
  const attention = data?.attention ?? []
  const events = (data?.recent_events ?? []).filter((event) => EVENT_LABELS[event.type]).slice(0, 5)

  return <div className="wb-page ov3-page">
    <PageHeader title="工程总览" description="先找到项目，再进入它的需求、方案、执行和交付。" actions={<>
      <button className="wb-button wb-button-secondary" onClick={() => load(true)}>{loading && data ? '更新中…' : '刷新'}</button>
      <Link className="wb-button wb-button-primary" to="/projects">管理项目 <span aria-hidden="true">→</span></Link>
    </>} />
    {error && <ErrorNotice message={data ? `刷新失败，以下为上一份记录：${error}` : error} />}
    {loading && !data && <div className="wb-card ov3-loading" role="status" aria-label="正在读取项目进展"><span /><span /><span /></div>}
    {data && <>
      {data.snapshot_at && <p className="wb-runtime-note">最近同步：{formatDate(data.snapshot_at)} · 每 5 秒更新</p>}
      <section className="ov3-summary" aria-label="全部项目摘要">
        <div><span>项目</span><strong>{data.projects}</strong><small>各自拥有独立的工程记录</small></div>
        <div><span>进行中的运行</span><strong>{data.active_runs}</strong><small>正在规划、执行或验证</small></div>
        <div className={data.attention_runs ? 'is-attention' : ''}><span>待处理</span><strong>{data.attention_runs}</strong><small>原因与处理入口见下方</small></div>
        <div><span>成果已就绪</span><strong>{data.engineering?.verified_runs ?? 0}</strong><small>已验证，可查看或下载</small></div>
      </section>
      {attention.length > 0 && <section className="wb-card pw-attention" aria-labelledby="overview-attention">
        <div className="ov3-section-head"><div><span className="wb-eyebrow">需要处理</span><h2 id="overview-attention">先解决阻止工作继续的问题</h2></div>
          <Link className="wb-text-link" to="/runs?filter=attention">全部 {data.attention_runs} 条 →</Link></div>
        <AttentionList items={attention.slice(0, 4)} />
      </section>}
      <section className="pw-portfolio" aria-labelledby="project-portfolio">
        <div className="ov3-section-head"><div><span className="wb-eyebrow">项目工作台</span><h2 id="project-portfolio">每个项目，一套完整的工程闭环</h2><p>阶段数量按当前计划记录统计，不等于验证通过数。点击阶段查看同一份进度。</p></div></div>
        {(data.project_summaries ?? []).map((project) => <article className="wb-card pw-project" key={project.id}>
          <header className="pw-project-heading"><div><Link to={`/projects/${encodeURIComponent(project.id)}`}><h3>{project.name}</h3></Link><p>{project.repository?.startsWith('local/') ? '系统管理的工作区' : project.repository}</p></div>
            <div className="pw-project-activity"><span>{project.active_runs} 项进行中</span>{project.attention_runs > 0 && <Link className="pw-warning-text" to={`/runs?project_id=${encodeURIComponent(project.id)}&filter=attention`}>{project.attention_runs} 项待处理</Link>}<Link className="wb-button wb-button-secondary" to={`/projects/${encodeURIComponent(project.id)}`}>进入项目 →</Link>{project.next_run && <Link className="wb-button wb-button-primary" to={nextRunAction(project.next_run).href}>{nextRunAction(project.next_run).label} →</Link>}</div>
          </header>
          <nav className="pw-project-stages" aria-label={`${project.name}的工程阶段`}>{PROJECT_STAGES.map((stage, index) => {
            const record = project.engineering.stages.find((item) => item.id === stage.id)
            const run = project.next_run
            const current = Boolean(run && !['cancelled', 'discarded'].includes(run.status) && runGuidance(run).stage === stage.id)
            const moving = Boolean(run && ['received', 'planning', 'queued', 'running', 'verifying', 'publishing'].includes(run.status))
            const waiting = Boolean(run && ['needs_human', 'needs_clarification', 'awaiting_approval', 'failed'].includes(run.status))
            const marker = moving ? '正在进行' : waiting ? '待处理' : run?.status === 'published' ? '已交付' : '可领取'
            return <Link to={projectStageHref(project.id, stage.id)} key={stage.id} aria-current={current ? 'step' : undefined} className={current ? `pw-stage-current ${moving ? 'is-moving' : waiting ? 'is-waiting' : 'is-ready'}` : undefined}>
              <span className="pw-stage-top"><span className="pw-stage-number">0{index + 1}</span>{current && <span className="pw-stage-marker"><i aria-hidden="true" />{marker}</span>}</span><strong>{stage.label}</strong>
              <span className="pw-stage-count">{record?.count ?? 0}<small>{record?.unit ?? '条记录'}</small></span>
              <small className="pw-stage-link">{stage.action} <span aria-hidden="true">↗</span></small>
            </Link>
          })}</nav>
          <footer className="pw-project-footer"><span>需求 → 方案 → 执行 → 验证 → 交付 → 经验回流</span>
            {user?.role !== 'member' ? <Link to={`/projects/${encodeURIComponent(project.id)}?tab=settings#project-budget`}>单次运行预算 {typeof project.budget_usd === 'number' ? `$${project.budget_usd.toFixed(2)}` : '未返回'} →</Link> : <span>项目配置由管理员维护</span>}
          </footer>
        </article>)}
        {data.project_summaries?.length === 0 && <div className="wb-card"><EmptyState title="登记第一个项目" description="每个项目会拥有自己的需求、执行记录、交付产物和经验。" action={<Link className="wb-button wb-button-primary" to="/projects">前往项目管理</Link>} /></div>}
        {!data.project_summaries && <div className="wb-card"><EmptyState title="项目进展暂未返回" action={<Link className="wb-button wb-button-secondary" to="/projects">查看项目列表</Link>} /></div>}
      </section>
      <section className="wb-card ov3-events" aria-labelledby="overview-events"><div className="ov3-section-head"><div><span className="wb-eyebrow">工作区动态</span><h2 id="overview-events">最近的工程进展</h2></div><Link className="wb-text-link" to="/costs">成本与调用记录 →</Link></div>
        {events.length ? <div className="ov3-event-list">{events.map((event) => <Link className="ov3-event-row" to={`/runs/${encodeURIComponent(event.run_id)}`} key={event.id}><span className="ov3-event-dot" aria-hidden="true" /><span><strong>{event.run_title || '运行进展'}</strong><small>{event.project_name} · {EVENT_LABELS[event.type]} · {formatDate(event.at)}</small></span><span className="ov3-arrow" aria-hidden="true">→</span></Link>)}</div> : <p className="wb-runtime-note">项目开始工作后，这里会汇总重要进展。</p>}
      </section>
    </>}
  </div>
}
