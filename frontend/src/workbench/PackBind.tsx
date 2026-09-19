import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { packsBase, type PackBinding } from './pack-types'

export default function PackBind({ packId, versionId, version, csrfToken, onUnauthorized, onChanged }: {
  packId: string; versionId: string; version: number; csrfToken: string;
  onUnauthorized: () => void; onChanged: () => void
}) {
  const [agents, setAgents] = useState<Array<{ id: string; name: string }>>([])
  const [target, setTarget] = useState('')
  const [bound, setBound] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const c = new AbortController()
    request<{ agents: Array<{ id: string; name: string }> }>('/api/v4/agents', { signal: c.signal, onUnauthorized })
      .then(value => setAgents(value.agents ?? [])).catch(e => { if (!c.signal.aborted) setError(String(e)) })
    return () => c.abort()
  }, [onUnauthorized])
  const bind = async () => {
    if (!target || busy) return
    setBusy(true); setError(''); setBound('')
    try {
      const current = await request<{ bindings: PackBinding[] }>(`${packsBase}/bindings/${encodeURIComponent(target)}`, { onUnauthorized })
      const existing = current.bindings.find(b => b.pack_id === packId)
      await request(`${packsBase}/bindings`, { method: 'POST', csrfToken, onUnauthorized,
        body: { agent_id: target, version_id: versionId, expected_revision: existing?.revision ?? 0 } })
      setBound(target); onChanged()
    } catch (cause) { setError(cause instanceof Error ? cause.message : '挂靠失败') }
    finally { setBusy(false) }
  }
  return <section className="pk-section">
    <h2>挂靠到职能体</h2>
    <p className="pk-muted">将 v{version} 安装为可调用工具。它只影响之后的新调用；既有任务保持原版本。</p>
    <label>目标职能体 <select value={target} disabled={busy} onChange={e => { setTarget(e.target.value); setBound('') }}>
      <option value="">请选择职能体</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
    </select></label>
    <button className="pk-button" disabled={!target || busy} onClick={() => void bind()}>{busy ? '挂靠中…' : `挂靠 v${version}`}</button>
    {error && <p role="alert">{error}</p>}
    {bound && <p role="status">已挂靠。<Link to={`/agents/${encodeURIComponent(bound)}/chat`}>打开职能体使用工具</Link></p>}
  </section>
}
