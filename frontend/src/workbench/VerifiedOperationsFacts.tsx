import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, errorText, formatDate } from './ui'
type Fact = { key: string; revision: number; kind: string; status: string; title: string; content: string; paths: string[]; commit_sha: string | null; provenance: { source?: string; run_id?: string; at?: string } }
export default function VerifiedOperationsFacts({ projectId, csrfToken, onUnauthorized, isAdmin }: { projectId: string; csrfToken: string; onUnauthorized: () => void; isAdmin: boolean }) {
  const [facts, setFacts] = useState<Fact[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const url = `/api/v2/projects/${encodeURIComponent(projectId)}/knowledge`
  useEffect(() => {
    const controller = new AbortController()
    void request<{ entries: Fact[] }>(url, { onUnauthorized, signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setFacts((data.entries || []).filter(f => f.status === 'active' && f.provenance.source === 'verified_operation'))
    }).catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [url, onUnauthorized])
  async function remove(fact: Fact) {
    setBusy(true); setError(null)
    try {
      const { kind, title, content, paths, commit_sha } = fact
      await request(`${url}/${encodeURIComponent(fact.key)}`, { method: 'PUT', csrfToken, onUnauthorized,
        body: { kind, title, content, paths, commit_sha, status: 'retired', expected_revision: fact.revision } })
      setFacts(items => items.filter(item => item.key !== fact.key))
    } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <div><h3>已验证的运维事实</h3>{!facts.length && <p>暂无已回流事实。</p>}
    {facts.map(fact => <article key={fact.key}><strong>{fact.title}</strong><p>{fact.content}</p><small>{formatDate(fact.provenance.at)}</small>{fact.provenance.run_id && <Link to={`/runs/${fact.provenance.run_id}?view=verification`}>查看来源证据</Link>}{isAdmin && <button disabled={busy} onClick={() => void remove(fact)}>删除事实</button>}</article>)}
    {error && <ErrorNotice message={error} />}
  </div>
}
