import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon, { CategoryBadge, packMark } from '../workbench/Icon'
import AgentMetadataEditor from '../workbench/AgentMetadataEditor'
import NativePackImport from '../workbench/NativePackImport'
import './conversation.css'

type Agent = { id: string; name: string; purpose?: string; builtin_pack?: string; active_version?: number; updated_at?: string }

/** Card-level "..." menu with keyboard navigation, Esc close, and correct focus return. */
function CardMenu({ agent, isAdmin, csrfToken, onUnauthorized, onUpdated }: {
  agent: Agent; isAdmin: boolean
  csrfToken: string; onUnauthorized: () => void
  onUpdated: (a: Agent) => void
}) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node) &&
          triggerRef.current && !triggerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  // Keyboard: Esc close + arrow navigation within menu
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); triggerRef.current?.focus(); return }
      if (!['ArrowDown', 'ArrowUp'].includes(e.key)) return
      e.preventDefault()
      const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
      const idx = items.indexOf(document.activeElement as HTMLElement)
      const next = e.key === 'ArrowDown' ? (idx + 1) % items.length : (idx - 1 + items.length) % items.length
      items[next]?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  // Focus first menu item on open
  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => {
        menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
      })
    }
  }, [open])

  // Return focus to trigger on close
  const prevOpen = useRef(open)
  useEffect(() => {
    if (prevOpen.current && !open && !editing) triggerRef.current?.focus()
    prevOpen.current = open
  }, [open, editing])

  if (editing) {
    return (
      <div className="cv-card-edit-wrap">
        <AgentMetadataEditor
          agent={agent}
          csrfToken={csrfToken}
          onUnauthorized={onUnauthorized}
          onSaved={(updated) => { setEditing(false); onUpdated(updated) }}
          onCancel={() => { setEditing(false); triggerRef.current?.focus() }}
        />
      </div>
    )
  }

  return (
    <div className="cv-card-menu-wrap" style={{ position: 'relative' }}>
      <button
        ref={triggerRef}
        className="cv-card-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="更多操作"
        onClick={() => setOpen(o => !o)}
      >
        &#x22EF;
      </button>
      {open && (
        <div ref={menuRef} className="cv-card-menu" role="menu" aria-label="职能体操作">
          {isAdmin && (
            <button role="menuitem" tabIndex={0} onClick={() => { setOpen(false); setEditing(true) }}>
              编辑名称与用途
            </button>
          )}
          {agent.builtin_pack && (
            <a role="menuitem" tabIndex={0}
              href={`/api/v4/builtin-packs/${encodeURIComponent(agent.builtin_pack)}/download`}>
              <Icon name="download" width={14} height={14} /> 下载职能包
            </a>
          )}
        </div>
      )}
    </div>
  )
}

/** 职能体目录：可浏览的岗位能力，直接开始对话或管理职能体。 */
export default function AgentCatalog({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const [agents, setAgents] = useState<Agent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [name, setName] = useState('')
  const [purpose, setPurpose] = useState('')
  const [busy, setBusy] = useState(false)
  const isAdmin = user?.role !== 'member'

  const loadAgents = useCallback((signal?: AbortSignal) => {
    return request<{ agents: Agent[] }>('/api/v4/agents', { onUnauthorized, signal })
      .then(r => setAgents(r.agents || []))
      .catch(cause => { if (!signal?.aborted) setError(errorText(cause)) })
  }, [onUnauthorized])

  useEffect(() => {
    const c = new AbortController()
    void loadAgents(c.signal)
    return () => c.abort()
  }, [loadAgents])

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

  const handleUpdated = (updated: Agent) => {
    setAgents(prev => prev?.map(a => a.id === updated.id ? { ...a, ...updated } : a) ?? null)
  }

  return (
    <div className="cv-page">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
        <div style={{ flex: 1 }}><h1>职能体</h1><p className="cv-page-sub">长期沉淀的专业角色。可以直接开始对话，或管理它的方法与能力。</p></div>
        {isAdmin && <div style={{ display: 'flex', gap: 8 }}>
          {/* Whole-pack import lives on the page users actually see. It was
              previously only on an unrouted list branch, so the instruction to
              "use the import button on the list" could not be followed. */}
          <button className="cv-btn cv-btn-secondary" aria-expanded={importing}
            onClick={() => { setImporting(v => !v); setCreating(false) }}>导入职能体</button>
          <button className="cv-btn cv-btn-primary" onClick={() => { setCreating(v => !v); setImporting(false) }}><Icon name="plus" width={15} height={15} /> 新建职能体</button>
        </div>}
      </div>
      {isAdmin && importing && <div className="cv-settings-section" style={{ padding: 16 }}>
        <NativePackImport csrfToken={csrfToken} onUnauthorized={onUnauthorized} user={user}
          onImported={(id) => { setImporting(false); navigate(`/agents/${encodeURIComponent(id)}`) }} />
      </div>}
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
              <div style={{ flex: 1 }}><strong>{agent.name}</strong><small>v{agent.active_version ?? '—'}</small></div>
              {isAdmin && <CardMenu agent={agent} isAdmin={isAdmin} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onUpdated={handleUpdated} />}
            </header>
            <p>{agent.purpose || '还没有用途说明。'}</p>
            <footer>
              <Link className="cv-btn cv-btn-primary" to={`/agents/${encodeURIComponent(agent.id)}/chat`}>开始对话 <Icon name="arrow" width={15} height={15} /></Link>
              {isAdmin && <Link className="cv-agent-dl" to={`/agents/${encodeURIComponent(agent.id)}`}>管理职能体</Link>}
            </footer>
          </article>
        ))}
      </div>}
    </div>
  )
}
