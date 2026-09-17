import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, formatDate, type PageProps } from './ui'
import { packsBase, type PackSummary } from './pack-types'
import './packs.css'

/** The pack catalogue, shown as a tab of the existing 能力库 page — not a second
 *  top-level entry. A load failure says so instead of rendering an empty catalogue
 *  that looks like "you have no capabilities yet". */
export default function PacksPage({ onUnauthorized }: PageProps & { embedded?: boolean }) {
  const [packs, setPacks] = useState<PackSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    request<{ packs: PackSummary[] }>(packsBase, { onUnauthorized, signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setPacks(value.packs); setError(null) } })
      .catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [onUnauthorized])

  if (error) return <div className="pk-notice pk-notice-error" role="alert"><strong>职能包目录加载失败</strong><p>{error}</p></div>
  if (!packs) return <p className="pk-muted">正在载入职能包…</p>
  if (packs.length === 0) return <div className="pk-empty"><span aria-hidden="true">○</span><strong>还没有职能包</strong>
    <p>描述你要建立的能力，开发完成后从任务成果里选出可复用的程序与方法，沉淀为职能包。</p>
    <Link className="pk-button" to="/">描述你要建立的能力</Link></div>

  return <ul className="pk-grid">{packs.map(pack => <li key={pack.id} className="pk-card">
    <div className="pk-card-head">
      <Link className="pk-card-title" to={`/ability-center/packs/${encodeURIComponent(pack.id)}`}>{pack.name}</Link>
      <span className={`pk-pill ${pack.published_version ? 'is-published' : 'is-draft'}`}>
        {pack.published_version ? `已发布 v${pack.published_version}` : '草稿'}</span>
    </div>
    <p className="pk-card-purpose">{pack.purpose || '尚未填写用途'}</p>
    <div className="pk-card-foot"><span>更新于 {formatDate(pack.updated_at)}</span>
      <Link className="pk-link" to={`/ability-center/packs/${encodeURIComponent(pack.id)}`}>查看能力 →</Link></div>
  </li>)}</ul>
}
