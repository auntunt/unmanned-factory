// 管理下钻：单个项目的任务摘要（不含对话）。
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, StatusBadge, errorText, formatDate, type PageProps } from '../workbench/ui'
import { managementApi } from './api'
import type { ProjectDetail } from './types'
import './management.css'

function money(value: number | null): string {
  return value === null ? '未记录' : `$${value.toFixed(2)}`
}

export default function ProjectDetailPage({ onUnauthorized }: PageProps) {
  const { projectId } = useParams<{ projectId: string }>()
  const [data, setData] = useState<ProjectDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (!projectId) return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true); setError(null); setStatus(null)
    managementApi.project(projectId, { onUnauthorized, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setData(value) })
      .catch((cause) => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError) setStatus(cause.status)
        setError(errorText(cause))
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [projectId, onUnauthorized])

  if (status === 403) return <div className="mg-page"><PageHeader title="项目" /><ErrorNotice message="你没有管理查看范围" /></div>
  if (status === 404) return <div className="mg-page"><PageHeader title="项目" /><ErrorNotice message="范围不存在或你无权查看" /></div>

  return <div className="mg-page">
    <div className="mg-topbar"><Link to="/management" className="mg-back-link">← 返回管理首页</Link></div>
    {loading && !data && <div role="status">加载中…</div>}
    {error && !data && <ErrorNotice message={error} />}
    {data && <>
      <PageHeader title={data.project.name} description={data.project.unit_path ? `所属组织：${data.project.unit_path}` : '未归属组织'} />
      <p className="mg-note">{data.note}</p>
      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">任务</span><h2>任务摘要{data.truncated ? '（仅显示前 100 条）' : ''}</h2></div></div>
        {data.runs.length === 0 ? <p className="mg-hint">该项目暂无任务记录。</p> : <div className="wb-table-wrap"><table className="wb-table">
          <thead><tr><th>标题</th><th>状态</th><th>发起人</th><th>更新时间</th><th>费用</th></tr></thead>
          <tbody>
            {data.runs.map((run) => (
              <tr key={String(run.run_id)}>
                <td>{run.can_act ? <Link to={`/runs/${encodeURIComponent(String(run.run_id))}`}>{run.title}</Link> : run.title}</td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.initiator || '未知'}</td>
                <td>{formatDate(run.updated_at)}</td>
                <td>{money(run.known_cost_usd)}{run.unknown_cost_calls > 0 ? ` · 另有 ${run.unknown_cost_calls} 次调用费用未知` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table></div>}
      </section>
    </>}
  </div>
}
