import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, errorText, formatDate, statusLabel } from './ui'
interface Inspection { enabled: boolean; interval_s: number; revision: number; last_at?: string; last_run_id?: string; last_status?: string; last_result?: string }
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
  const save = async (enabled: boolean, interval_s: number) => {
    if (!config || busy) return
    setBusy(true); setError(null)
    try { setConfig(await request<Inspection>(url, { method: 'PUT', csrfToken, onUnauthorized, body: { enabled, interval_s, revision: config.revision } })) }
    catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <section className="wb-card"><h2>本机定时巡检</h2><p>只检查项目的隔离副本并记录结果，不改代码、不部署，不含远程服务器。首次巡检在一个间隔后入队。</p>
    {config && <><label className="wb-checkbox"><input type="checkbox" checked={config.enabled} disabled={!isAdmin || busy} onChange={e => void save(e.target.checked, config.interval_s)} />启用巡检</label><label>巡检间隔<select disabled={!isAdmin || busy} value={config.interval_s} onChange={e => void save(config.enabled, Number(e.target.value))}>{[...new Set([300, 900, 3600, 21600, 86400, config.interval_s])].sort((a,b) => a-b).map(seconds => <option key={seconds} value={seconds}>{seconds % 86400 === 0 ? `${seconds / 86400} 天` : seconds % 3600 === 0 ? `${seconds / 3600} 小时` : `${seconds / 60} 分钟`}</option>)}</select></label>
    <p>上次巡检：{config.last_at ? formatDate(config.last_at) : '尚未运行'}{config.last_status && ` · ${statusLabel(config.last_status)}`}</p>{config.last_result && <p>{config.last_result}</p>}{config.last_run_id && <Link to={`/runs/${config.last_run_id}?view=verification`}>查看巡检证据 →</Link>}</>}
    {error && <ErrorNotice message={error} />}</section>
}
