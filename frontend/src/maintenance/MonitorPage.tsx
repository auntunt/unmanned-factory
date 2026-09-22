// 运维监控：数字大屏 / 工作画布。只渲染页面内容，外框（三级入口）由子系统壳负责。
import { useCallback, useEffect, useRef, useState } from 'react'
import { useMaintenancePath } from './base-path'
import { Link, useNavigate } from 'react-router-dom'

import { EmptyState, ErrorNotice, PageHeader, errorText, formatDate } from '../workbench/ui'
import { WorkspaceApiError } from '../workspace/api'
import type { PageProps } from '../workbench/ui'
import { maintenanceApi } from './api'
import SyntheticBadge from './SyntheticBadge'
import type { AttentionKind, DataSource, Graph, Overview, ProjectRow, SourceStatus } from './types'
import { ATTENTION_LABEL } from './types'
import RelationCanvas from './RelationCanvas'
import './monitor.css'

const POLL_INTERVAL_MS = 15000

const SOURCE_STATUS_LABEL: Record<SourceStatus, string> = {
  connected: '已连接',
  online: '执行器在线',
  offline: '执行器离线',
  unknown: '未知',
  not_connected: '未接入',
  error: '异常',
}

function sourceTone(status: SourceStatus): string {
  if (status === 'connected' || status === 'online') return 'success'
  if (status === 'offline' || status === 'error') return 'danger'
  if (status === 'unknown') return 'warning'
  return 'neutral'
}

function sourceLabel(source: DataSource | undefined): string {
  if (!source) return SOURCE_STATUS_LABEL.unknown
  return SOURCE_STATUS_LABEL[source.status] ?? source.status
}

function preferNonUnknown(primary: DataSource | undefined, fallback: DataSource | undefined): DataSource | undefined {
  if (primary && primary.status !== 'unknown' && primary.status !== 'not_connected') return primary
  return fallback ?? primary
}

function metricValue(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value)
}

function serviceStatusLabel(value: string | undefined): string {
  if (!value || value === 'not_connected') return '未接入'
  return value
}

function windowLabel(kind: string): string {
  return kind === 'day' ? '今日' : kind
}

export default function MonitorPage(props: PageProps & { projectId?: string }) {
  const mp = useMaintenancePath()
  const { projectId, onUnauthorized } = props
  const navigate = useNavigate()

  const [view, setView] = useState<'grid' | 'canvas'>('grid')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(true)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [staleSince, setStaleSince] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const [graph, setGraph] = useState<Graph | null>(null)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState<string | null>(null)

  const [fullscreen, setFullscreen] = useState(false)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const hasDataRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    async function load() {
      try {
        const data = await maintenanceApi.overview(projectId, { onUnauthorized, signal: controller.signal })
        if (cancelled) return
        setOverview(data)
        hasDataRef.current = true
        setStaleSince(null)
        setOverviewError(null)
        setForbidden(false)
      } catch (err) {
        if (cancelled || (err instanceof DOMException && err.name === 'AbortError')) return
        if (err instanceof WorkspaceApiError && err.status === 403) {
          setForbidden(true)
          return
        }
        if (hasDataRef.current) {
          setStaleSince(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
        } else {
          setOverviewError(errorText(err))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    async function tick() {
      await load()
      if (!cancelled) timer = setTimeout(tick, POLL_INTERVAL_MS)
    }
    setLoading(!hasDataRef.current)
    tick()

    return () => {
      cancelled = true
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [projectId, onUnauthorized, reloadKey])

  useEffect(() => {
    if (view !== 'canvas') return
    let cancelled = false
    const controller = new AbortController()
    setGraphLoading(true)
    setGraphError(null)
    maintenanceApi.graph(projectId, { onUnauthorized, signal: controller.signal })
      .then(data => { if (!cancelled) setGraph(data) })
      .catch(err => {
        if (cancelled || (err instanceof DOMException && err.name === 'AbortError')) return
        setGraphError(errorText(err))
      })
      .finally(() => { if (!cancelled) setGraphLoading(false) })
    return () => { cancelled = true; controller.abort() }
  }, [view, projectId, onUnauthorized])

  useEffect(() => {
    function onChange() { setFullscreen(!!document.fullscreenElement) }
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  useEffect(() => {
    if (!fullscreen || document.fullscreenElement) return
    function onKey(e: KeyboardEvent) { if (e.key === 'Escape') setFullscreen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fullscreen])

  const toggleFullscreen = useCallback(async () => {
    const el = containerRef.current
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen()
      } else if (el?.requestFullscreen) {
        await el.requestFullscreen()
      } else {
        setFullscreen(f => !f)
      }
    } catch {
      setFullscreen(f => !f)
    }
  }, [])

  const retry = () => setReloadKey(k => k + 1)
  const openTask = useCallback((taskId: string) => navigate(mp(`/${taskId}`)), [navigate, mp])

  if (forbidden) {
    return (
      <div className="mn-page">
        <PageHeader title="运维监控" description="先看全局，再进入需要处理的现场。" />
        <EmptyState title="无权限" description="当前身份没有查看运维监控数据的权限。" />
      </div>
    )
  }

  if (loading && !overview) {
    return (
      <div className="mn-page">
        <PageHeader title="运维监控" description="先看全局，再进入需要处理的现场。" />
        <div className="wb-loading-card"><span className="wb-spinner" aria-hidden="true" />正在加载监控数据…</div>
      </div>
    )
  }

  if (!overview) {
    return (
      <div className="mn-page">
        <PageHeader title="运维监控" description="先看全局，再进入需要处理的现场。" />
        <ErrorNotice message={overviewError ?? '监控数据加载失败'} />
        <button type="button" className="wb-button" onClick={retry}>重试</button>
      </div>
    )
  }

  const noProjects = overview.counts.projects === 0 || overview.scope.project_ids.length === 0

  const partialErrorFor = (section: string) => overview.partial_errors.find(p => p.section === section)?.message

  return (
    <div ref={containerRef} className={`mn-page${fullscreen ? ' mn-fullscreen' : ''}`}>
      <PageHeader
        title="运维监控"
        description="先看全局，再进入需要处理的现场。"
        actions={(
          <div className="mn-viewbar">
            <button type="button" className={`wb-button${view === 'grid' ? ' wb-button-primary' : ''}`} onClick={() => setView('grid')}>数字大屏</button>
            <button type="button" className={`wb-button${view === 'canvas' ? ' wb-button-primary' : ''}`} onClick={() => setView('canvas')}>工作画布</button>
            <button type="button" className="wb-button" onClick={toggleFullscreen}>全屏展示</button>
          </div>
        )}
      />

      <div className="mn-sources">
        <span className={`wb-status wb-status-${sourceTone(preferNonUnknown(overview.sources.executor, overview.sources.maintenance)?.status ?? 'unknown')}`}>
          <i aria-hidden="true" />维护执行 · {sourceLabel(preferNonUnknown(overview.sources.executor, overview.sources.maintenance))}
        </span>
        <span className={`wb-status wb-status-${sourceTone(overview.sources.server_metrics.status)}`}>
          <i aria-hidden="true" />服务器指标 · {sourceLabel(overview.sources.server_metrics)}
        </span>
        <span className={`wb-status wb-status-${sourceTone(preferNonUnknown(overview.sources.app_probes, overview.sources.alerts)?.status ?? 'unknown')}`}>
          <i aria-hidden="true" />应用告警/探针 · {sourceLabel(preferNonUnknown(overview.sources.app_probes, overview.sources.alerts))}
        </span>
      </div>
      <p className="mn-hint">
        数据时间：{formatDate(overview.generated_at)} · 统计窗口：{windowLabel(overview.window.kind)}（{overview.window.timezone}）
      </p>

      {staleSince && (
        <div className="mn-banner" role="status">
          <strong>连接中断</strong>
          <span>显示的是 {staleSince} 的数据，已过期。</span>
          <button type="button" className="wb-button" onClick={retry}>重试</button>
        </div>
      )}

      {noProjects
        ? (
          <EmptyState
            title="还没有接入代码库"
            description="先登记要维护的项目，才能看到运维监控数据。"
            action={<Link className="wb-button wb-button-primary" to={mp('/repos')}>去接入代码库</Link>}
          />
        )
        : (
          <>
            <div className="mn-metrics">
              <div className="wb-stat-card">
                <span>维护项目</span>
                <strong>{metricValue(overview.counts.projects)}</strong>
                <small>代码库与任务关联</small>
              </div>
              <div className="wb-stat-card">
                <span>正在执行</span>
                <strong>{metricValue(overview.counts.running)}</strong>
                <small>计划、修改或检查中</small>
              </div>
              <div className="wb-stat-card">
                <span>需要处理</span>
                <strong>{metricValue(overview.counts.attention?.total ?? null)}</strong>
                <small>
                  业务待答 {metricValue(overview.counts.attention?.answer ?? null)} · 待批准 {metricValue(overview.counts.attention?.approval ?? null)} · 执行受阻 {metricValue(overview.counts.attention?.blocked ?? null)}
                </small>
              </div>
              <div className="wb-stat-card">
                <span>今日已交付</span>
                <strong>{metricValue(overview.counts.delivered_in_window)}</strong>
                <small>产物就绪 ≠ 已上线</small>
              </div>
            </div>

            {view === 'grid'
              ? (
                <div className="mn-split">
                  <div className="mn-col">
                    <div className="wb-card">
                      <div className="wb-card-head"><h2>当前需要你关注</h2></div>
                      {partialErrorFor('attention') && <p className="mn-hint">部分数据不可用：{partialErrorFor('attention')}</p>}
                      {overview.attention.length === 0
                        ? <EmptyState title="暂无待处理事项" />
                        : (
                          <div className="wb-action-list">
                            {overview.attention.map(item => (
                              <div className="wb-action-row mn-attention-row" key={item.task_id}>
                                <div className="mn-attention-copy">
                                  <strong>{item.project_name} · {item.title}</strong>
                                  <span>{attentionBadge(item.kind)}</span>
                                  <span className="mn-attention-reason">{item.reason}</span>
                                </div>
                                <Link className="wb-text-link" to={mp(`/${item.task_id}`)}>进入现场</Link>
                              </div>
                            ))}
                          </div>
                        )}
                    </div>

                    <div className="wb-card">
                      <div className="wb-card-head"><h2>维护项目概览</h2></div>
                      {partialErrorFor('projects') && <p className="mn-hint">部分数据不可用：{partialErrorFor('projects')}</p>}
                      {overview.projects.length === 0
                        ? <EmptyState title="还没有接入代码库" action={<Link className="wb-text-link" to={mp('/repos')}>去接入代码库</Link>} />
                        : (
                          <div className="wb-table-wrap">
                            <table className="wb-table">
                              <thead>
                                <tr><th>代码库</th><th>接入状态</th><th>执行/等待</th><th>已交付</th><th>交付目标</th><th>服务状态</th></tr>
                              </thead>
                              <tbody>
                                {overview.projects.map(row => <ProjectRowView key={row.project_id} row={row} />)}
                              </tbody>
                            </table>
                          </div>
                        )}
                    </div>
                  </div>

                  <div className="mn-col">
                    <div className="wb-card">
                      <div className="wb-card-head"><h2>最新工作事件</h2></div>
                      {partialErrorFor('events') && <p className="mn-hint">部分数据不可用：{partialErrorFor('events')}</p>}
                      {overview.events.length === 0
                        ? <EmptyState title="暂无最新事件" />
                        : overview.events.map(ev => (
                          <div className="mn-event" key={`${ev.task_id}-${ev.sequence}`}>
                            <Link to={mp(`/${ev.task_id}`)}>{ev.label}</Link>
                            <small>{formatDate(ev.at)}</small>
                          </div>
                        ))}
                    </div>

                    <div className="wb-card">
                      <div className="wb-card-head"><h2>资源与运行情况</h2></div>
                      <dl className="mn-fact-list">
                        <dt>工作队列</dt>
                        <dd>待领取 {metricValue(overview.queue.pending)} · 执行中 {metricValue(overview.queue.running)}</dd>
                        <dt>执行器</dt>
                        <dd>{sourceLabel(overview.queue.executor)}{overview.queue.executor.detail ? ` · ${overview.queue.executor.detail}` : ''}</dd>
                        <dt>CPU / 内存</dt>
                        <dd>{overview.sources.server_metrics.status === 'not_connected' ? '未接入' : (overview.sources.server_metrics.detail ?? sourceLabel(overview.sources.server_metrics))}</dd>
                        <dt>应用可用性</dt>
                        <dd>{overview.sources.app_probes.status === 'not_connected' ? '未接入' : (overview.sources.app_probes.detail ?? sourceLabel(overview.sources.app_probes))}</dd>
                      </dl>
                    </div>
                  </div>
                </div>
              )
              : (
                <div className="wb-card">
                  {graphLoading && !graph && <div className="wb-loading-card"><span className="wb-spinner" aria-hidden="true" />正在加载工作画布…</div>}
                  {graphError && !graph && <ErrorNotice message={graphError} />}
                  {graph && <RelationCanvas graph={graph} onOpenTask={openTask} />}
                </div>
              )}
          </>
        )}
    </div>
  )
}

function attentionBadge(kind: AttentionKind): string {
  return ATTENTION_LABEL[kind]
}

function ProjectRowView({ row }: { row: ProjectRow }) {
  return (
    <tr>
      <td>
        {row.name} <SyntheticBadge synthetic={row.synthetic} />
        <small>{row.repository}</small>
      </td>
      <td><span className="wb-status">{row.repo_state_label}</span></td>
      <td>{row.running} / {row.waiting}</td>
      <td>{row.delivered}</td>
      <td>{row.delivery_target}</td>
      <td>{serviceStatusLabel(row.service_status)}</td>
    </tr>
  )
}
