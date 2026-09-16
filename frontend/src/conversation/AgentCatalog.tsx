import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon, { CategoryBadge, packMark } from '../workbench/Icon'
import './conversation.css'

type Agent = { id: string; name: string; purpose?: string; builtin_pack?: string; active_version?: number }

/** 职能体目录：可浏览的岗位能力，直接开始对话或维护方法。
 *  就绪状态不靠 active_version 断言（有版本不等于工具就绪）；日常制作也会沿用项目已配置的能力。 */
export default function AgentCatalog({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const [agents, setAgents] = useState<Agent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [purpose, setPurpose] = useState('')
  const [busy, setBusy] = useState(false)
  const isAdmin = user?.role !== 'member'

  useEffect(() => {
    const c = new AbortController()
    request<{ agents: Agent[] }>('/api/v4/agents', { onUnauthorized, signal: c.signal })
      .then(r => setAgents(r.agents || []))
      .catch(cause => { if (!c.signal.aborted) setError(errorText(cause)) })
    return () => c.abort()
  }, [onUnauthorized])

  const create = async (event: FormEvent) => {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true); setError(null)
    try {
      const agent = await request<Agent>('/api/v4/agents', { method: 'POST', csrfToken, onUnauthorized, body: { name: name.trim(), purpose: purpose.trim(), identity: purpose.trim() } })
      setName(''); setPurpose(''); setCreating(false)
      navigate(`/agents/${encodeURIComponent(agent.id)}/chat`)
    } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }

  return (
    <div className="cv-page">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
        <div style={{ flex: 1 }}><h1>职能体</h1><p className="cv-page-sub">长期沉淀的专业角色。可以直接开始对话，或维护它的方法。</p></div>
        {isAdmin && <button className="cv-btn cv-btn-primary" onClick={() => setCreating(v => !v)}><Icon name="plus" width={15} height={15} /> 新建职能体</button>}
      </div>
      {creating && <form className="cv-settings-section" onSubmit={create} style={{ padding: 16 }}>
        <h2>新建职能体</h2>
        <label style={{ display: 'grid', gap: 6, margin: '8px 0' }}>名称<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} placeholder="例如：报价助手" style={{ padding: '9px 12px', border: '1px solid var(--cv-border)', borderRadius: 8, background: 'var(--cv-surface)', color: 'var(--cv-ink)' }} /></label>
        <label style={{ display: 'grid', gap: 6, margin: '8px 0' }}>用途（可选）<textarea maxLength={500} rows={2} value={purpose} onChange={e => setPurpose(e.target.value)} placeholder="它擅长解决哪些问题？" style={{ padding: '9px 12px', border: '1px solid var(--cv-border)', borderRadius: 8, background: 'var(--cv-surface)', color: 'var(--cv-ink)', font: 'inherit' }} /></label>
        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}><button className="cv-btn cv-btn-primary" disabled={busy || !name.trim()}>{busy ? '创建中…' : '创建并开始对话'}</button><button type="button" className="cv-btn cv-btn-secondary" onClick={() => setCreating(false)}>取消</button></div>
      </form>}
      {error && <div className="cv-error" role="alert"><span>{error}</span></div>}
      {agents === null && !error && <div className="cv-loading"><span className="cv-spinner" />正在读取…</div>}
      {agents && agents.length === 0 && !creating && <div className="cv-page-empty">还没有职能体。{isAdmin && '点右上角新建一个。'}</div>}
      {agents && agents.length > 0 && <div className="cv-catalog" style={{ marginTop: 16 }}>
        {agents.map(agent => (
          <article className="cv-agent-card" key={agent.id}>
            <header>
              <CategoryBadge avatar={Array.from(agent.name)[0]} mark={packMark(agent)} />
              <div><strong>{agent.name}</strong><small>v{agent.active_version ?? '—'}</small></div>
            </header>
            <p>{agent.purpose || '还没有用途说明。'}</p>
            <footer>
              <Link className="cv-btn cv-btn-primary" to={`/agents/${encodeURIComponent(agent.id)}/chat`}>开始对话 <Icon name="arrow" width={15} height={15} /></Link>
              <Link className="cv-agent-dl" to={`/agents/${encodeURIComponent(agent.id)}?mode=maintain`}>维护方法</Link>
              {agent.builtin_pack && <a className="cv-agent-dl" title="下载平台原始模板" href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}><Icon name="download" width={14} height={14} /> 职能包</a>}
            </footer>
          </article>
        ))}
      </div>}
    </div>
  )
}
