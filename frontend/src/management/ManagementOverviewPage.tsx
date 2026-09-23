// 管理首页：负责人/管理员的只读管理范围总览。契约见
// docs/implementation/enterprise-governance-v1/CONTRACT.md。
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, formatDate, type PageProps } from '../workbench/ui'
import { managementApi } from './api'
import type { Overview } from './types'
import './management.css'

const WINDOWS = [7, 30, 90] as const

function money(value: number | null, recorded: boolean): string {
  if (!recorded || value === null) return '未记录'
  return `$${value.toFixed(2)}`
}

function tokens(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value)
}

const AUDIT_LABEL: Record<string, string> = {
  'org.unit.created': '新建组织', 'org.unit.updated': '修改组织', 'org.unit.deleted': '删除组织',
  'org.project.bound': '项目绑定组织', 'org.project.unbound': '项目取消绑定',
  'org.scope.granted': '授予管理查看范围', 'org.scope.revoked': '撤销管理查看范围',
}

export default function ManagementOverviewPage({ onUnauthorized }: PageProps) {
  const [unitId, setUnitId] = useState<string | undefined>(undefined)
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true); setError(null); setStatus(null)
    managementApi.overview(unitId, days, { onUnauthorized, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setData(value) })
      .catch((cause) => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError) setStatus(cause.status)
        setError(errorText(cause))
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [unitId, days, onUnauthorized])

  if (status === 403) {
    return <div className="mg-page"><PageHeader title="管理" /><ErrorNotice message="你没有管理查看范围" /></div>
  }
  if (status === 404) {
    return <div className="mg-page"><PageHeader title="管理" /><ErrorNotice message="范围不存在或你无权查看" /></div>
  }

  return <div className="mg-page">
    <div className="mg-topbar">
      <div className="mg-topbar-left">
        <Link to="/" className="mg-back-link">← 返回工作台</Link>
        {data && <span className="mg-scope-label">当前范围：{data.scope.label}</span>}
      </div>
      {data?.scope.admin && <Link to="/management/org" className="wb-text-link">组织与授权 →</Link>}
    </div>
    <PageHeader title="管理" description="只读管理视图：不包含执行、取消、批准或读取对话的权限，这些仍按项目分配与发起人判断。" />
    {error && !data && <ErrorNotice message={error} />}
    {loading && !data && <div className="wb-loading" role="status">加载中…</div>}
    {data && <>
      <div className="mg-window-tabs" role="tablist" aria-label="时间窗口">
        {WINDOWS.map((w) => (
          <button key={w} type="button" className={`mg-window-tab ${days === w ? 'is-active' : ''}`} onClick={() => setDays(w)}>近 {w} 天</button>
        ))}
      </div>
      <p className="mg-generated">数据生成于 {formatDate(data.generated_at)}</p>

      {data.scope.units.length > 0 && <div className="mg-units">
        <button type="button" className={`mg-unit-chip ${unitId === undefined ? 'is-active' : ''}`} onClick={() => setUnitId(undefined)}>全部范围</button>
        {data.scope.units.map((u) => (
          <button key={u.id} type="button" className={`mg-unit-chip ${unitId === u.id ? 'is-active' : ''}`} onClick={() => setUnitId(u.id)}>
            {u.kind_label} · {u.path || u.name}
          </button>
        ))}
      </div>}

      <div className="mg-stat-grid">
        <div className="wb-stat-card"><span>进行中</span><strong>{data.counts.in_progress}</strong></div>
        <div className="wb-stat-card"><span>待处理</span><strong>{data.counts.pending}</strong></div>
        <div className="wb-stat-card"><span>成果就绪</span><strong>{data.counts.ready}</strong></div>
        <div className="wb-stat-card"><span>已发布</span><strong>{data.counts.published}</strong><small>{data.notes.published}</small></div>
        <div className="wb-stat-card"><span>失败</span><strong>{data.counts.failed}</strong></div>
      </div>

      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">项目</span><h2>范围内项目</h2></div></div>
        {data.projects.length === 0 ? <p className="mg-hint">当前范围内没有项目。</p> : data.projects.map((p) => (
          <div className="mg-project-row" key={p.id}>
            <div className="mg-project-head">
              <Link to={`/management/projects/${encodeURIComponent(p.id)}`}>{p.name}</Link>
              <span className="mg-hint">最近活动：{formatDate(p.last_activity_at)}</span>
            </div>
            <p className="mg-project-meta">{p.unit_path || '未归属组织'} · 发起人：{p.initiators.length > 0 ? p.initiators.join('、') : '无'}</p>
            <div className="mg-project-counts">
              <span>进行中 {p.counts.in_progress}</span><span>待处理 {p.counts.pending}</span>
              <span>成果就绪 {p.counts.ready}</span><span>已发布 {p.counts.published}</span><span>失败 {p.counts.failed}</span>
            </div>
          </div>
        ))}
      </section>

      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">待处理</span><h2>需要发起人或管理员处理</h2></div></div>
        {data.pending.length === 0 ? <p className="mg-hint">当前范围内没有待处理任务。</p> : data.pending.map((item) => (
          <div className="mg-pending-row" key={String(item.run_id)}>
            <strong>{item.title}</strong>
            <p className="mg-pending-meta">{item.project_name} · {item.reason} · 发起人：{item.initiator || '未知'} · {formatDate(item.updated_at)}</p>
            {item.can_act ? <Link to={`/runs/${encodeURIComponent(String(item.run_id))}`} className="wb-text-link">去工作台处理 →</Link>
              : <span className="mg-hint">{item.action_hint}</span>}
          </div>
        ))}
      </section>

      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">用量</span><h2>近 {data.usage.window_days} 天费用与 token</h2></div></div>
        <div className="mg-usage-grid">
          <div><span>已知费用</span><strong>{money(data.usage.known_cost_usd, data.usage.recorded)}</strong></div>
          <div><span>调用数</span><strong>{data.usage.calls}</strong></div>
          {data.usage.unknown_cost_calls > 0 && <div><span>费用未知</span><strong>另有 {data.usage.unknown_cost_calls} 次调用费用未知</strong></div>}
          <div><span>本月已结算 tokens</span><strong>{data.usage.tokens.recorded ? tokens(data.usage.tokens.settled_tokens) : '未记录'}</strong></div>
        </div>
        <p className="mg-source">费用来源：{data.usage.cost_source} · token 来源：{data.usage.tokens.source}</p>
      </section>

      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">审计</span><h2>组织与授权变更</h2></div></div>
        {data.audit.length === 0 ? <p className="mg-hint">没有相关审计记录。</p> : data.audit.map((row) => (
          <div className="mg-audit-row" key={row.id}>
            <span>{formatDate(row.at)} · {row.actor} · {AUDIT_LABEL[row.action] ?? row.action}</span>
            <small>
              {typeof row.data.unit_path === 'string' && `组织：${row.data.unit_path} `}
              {typeof row.data.target_username === 'string' && `对象：${row.data.target_username} `}
              {typeof row.data.project_name === 'string' && `项目：${row.data.project_name} `}
              {typeof row.data.result === 'string' && `结果：${row.data.result}`}
            </small>
          </div>
        ))}
      </section>

      {data.unassigned_projects && <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">未归属</span><h2>未归属组织的项目</h2><p>仅管理员可见。</p></div></div>
        <ul className="mg-unassigned">
          {data.unassigned_projects.map((p) => <li key={p.id}>{p.name}</li>)}
        </ul>
      </section>}
    </>}
  </div>
}
