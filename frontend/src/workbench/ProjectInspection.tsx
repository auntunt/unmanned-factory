import { LoadingCard } from './presentation'
import type { CSSProperties } from 'react'
import VerifiedOperationsFacts from './VerifiedOperationsFacts'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, errorText, formatDate, statusLabel } from './ui'
interface Inspection { remote_read_only?: boolean; history?: { run_id: string; at: string; verdict: 'pass' | 'fail' | 'unverified'; duration_s: number; failure_category?: string }[]; consecutive_failures?: number; enabled: boolean; interval_s: number; revision: number; last_at?: string; last_run_id?: string; last_status?: string; last_result?: string }
export default function ProjectInspection({ projectId, csrfToken, onUnauthorized, isAdmin }: { projectId: string; csrfToken: string; onUnauthorized: () => void; isAdmin: boolean }) {
  const [config, setConfig] = useState<Inspection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const url = `/api/v2/projects/${encodeURIComponent(projectId)}/inspection`
  useEffect(() => {
    const controller = new AbortController()
    const load = () => request<Inspection>(url, { onUnauthorized, signal: controller.signal }).then(data => { if (!controller.signal.aborted) setConfig(data) }).catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)) })
    void load(); const timer = window.setInterval(() => { if (document.visibilityState === 'visible' && !busy) void load() }, 15000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [url, onUnauthorized, busy])
  const save = async (enabled: boolean, interval_s: number, remote_read_only = config?.remote_read_only ?? false) => {
    if (!config || busy) return
    setBusy(true); setError(null)
    try { setConfig(await request<Inspection>(url, { method: 'PUT', csrfToken, onUnauthorized, body: { enabled, interval_s, remote_read_only, revision: config.revision } })) }
    catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <section className="wb-card wb-inspection-card"><div className="wb-inspection-head"><h2>定时巡检</h2><details className="wb-help"><summary aria-label="巡检帮助">? 巡检说明</summary><p>只检查项目的隔离副本并记录结果，不改代码、不部署；首次巡检在一个间隔后入队。</p><label className="wb-checkbox"><input type="checkbox" checked={config?.remote_read_only ?? false} disabled={!config || !isAdmin || busy} onChange={e => config && void save(config.enabled, config.interval_s, e.target.checked)} />远程只读探测（默认关闭）</label><p>开启后对绑定服务器执行已登记的只读检查。</p></details></div>
    {!config && !error && <LoadingCard label="正在读取巡检配置" />}
    {config && <><div className="wb-inspection-controls"><label className="wb-checkbox"><input type="checkbox" role="switch" checked={config.enabled} disabled={!isAdmin || busy} onChange={e => void save(e.target.checked, config.interval_s)} />启用巡检</label><label>巡检间隔<select disabled={!isAdmin || busy} value={config.interval_s} onChange={e => void save(config.enabled, Number(e.target.value))}>{[...new Set([300, 900, 3600, 21600, 86400, config.interval_s])].sort((a,b) => a-b).map(seconds => <option key={seconds} value={seconds}>{seconds % 86400 === 0 ? `${seconds / 86400} 天` : seconds % 3600 === 0 ? `${seconds / 3600} 小时` : `${seconds / 60} 分钟`}</option>)}</select></label></div>
    <p>上次巡检：{config.last_at ? formatDate(config.last_at) : '尚未运行'}{config.last_status && ` · ${statusLabel(config.last_status)}`}</p>
    {config.last_result && <details className="wb-help"><summary>结果摘要</summary><p>{config.last_result}</p></details>}
    <InspectionTrend history={config.history ?? []} consecutiveFailures={config.consecutive_failures ?? 0} />
    {config.last_run_id && <Link className="wb-text-link" to={`/runs/${config.last_run_id}?view=verification`}>查看巡检证据</Link>}</>}
    <VerifiedOperationsFacts projectId={projectId} csrfToken={csrfToken} onUnauthorized={onUnauthorized} isAdmin={isAdmin} />
    {error && <ErrorNotice message={error} />}</section>
}

const verdicts = { pass: { label: '通过', color: 'var(--color-success)' }, fail: { label: '失败', color: 'var(--color-danger)' }, unverified: { label: '未验证', color: 'var(--color-warning)' } }
export function InspectionTrend({ history, consecutiveFailures }: { history: NonNullable<Inspection['history']>; consecutiveFailures: number }) {
  return <div aria-label="最近 20 次巡检"><p>连续未通过：{consecutiveFailures} 次 {consecutiveFailures >= 3 && <strong className="wb-outage">持续故障</strong>}</p><div className="wb-inspection-trend">{[...history].sort((a,b) => b.at.localeCompare(a.at)).slice(0,20).reverse().map(item => {
    const date = new Date(item.at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
    const label = `${date} · ${verdicts[item.verdict].label} · ${item.duration_s.toFixed(1)}s`
    return <Link key={item.run_id} to={`/runs/${item.run_id}?view=verification`} aria-label={label} title={label} style={{ '--result-color': verdicts[item.verdict].color } as CSSProperties} />
  })}</div><div className="wb-inspection-legend">{Object.entries(verdicts).map(([key, value]) => <span key={key}><i aria-hidden="true" style={{ '--result-color': value.color } as CSSProperties} />{value.label}</span>)}</div></div>
}
