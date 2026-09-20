import { useEffect, useRef, useState, type FormEvent } from 'react'
import { request, WorkspaceApiError } from '../workspace/api'
import { errorText, type PageProps } from './ui'

type Agent = { id: string; name: string; purpose?: string; updated_at?: string }

/**
 * Shared inline editor for agent name and purpose.
 * Used from both the catalog card menu and the management page basic-info section.
 *
 * - Cancel restores original values.
 * - Request failure preserves user input.
 * - Saving disables the submit button (防重复提交).
 * - Calls PATCH /api/v4/agents/{aid}/metadata with { name, purpose, expected_updated_at }.
 */
export default function AgentMetadataEditor({
  agent, csrfToken, onUnauthorized, onSaved, onCancel,
}: Pick<PageProps, 'csrfToken' | 'onUnauthorized'> & {
  agent: Agent
  onSaved: (updated: Agent) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(agent.name)
  const [purpose, setPurpose] = useState(agent.purpose || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => { nameRef.current?.focus() }, [])

  const save = async (event: FormEvent) => {
    event.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) { setError('名称不能为空。'); return }
    if (trimmed.length > 120) { setError('名称最多 120 个字符。'); return }
    if (purpose.length > 4000) { setError('用途说明最多 4000 个字符。'); return }
    if (busy) return
    setBusy(true); setError(null)
    try {
      const updated = await request<Agent>(
        `/api/v4/agents/${encodeURIComponent(agent.id)}/metadata`,
        {
          method: 'PATCH', csrfToken, onUnauthorized,
          body: { name: trimmed, purpose: purpose.trim(), expected_updated_at: agent.updated_at },
        },
      )
      onSaved(updated)
    } catch (cause) {
      if (cause instanceof WorkspaceApiError && cause.status === 409) {
        setError('其他人刚刚修改了这个职能体，请刷新后重试。')
      } else {
        setError(errorText(cause))
      }
      // Keep user input on failure
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="cv-metadata-editor" onSubmit={save} data-testid="metadata-editor">
      <label style={{ display: 'grid', gap: 6 }}>
        名称
        <input
          ref={nameRef}
          required
          maxLength={120}
          value={name}
          onChange={e => setName(e.target.value)}
          disabled={busy}
          style={{ padding: '9px 12px', border: '1px solid var(--cv-border)', borderRadius: 8, background: 'var(--cv-surface)', color: 'var(--cv-ink)', font: 'inherit' }}
        />
      </label>
      <label style={{ display: 'grid', gap: 6, marginTop: 8 }}>
        用途
        <textarea
          maxLength={4000}
          rows={3}
          value={purpose}
          onChange={e => setPurpose(e.target.value)}
          disabled={busy}
          placeholder="它擅长解决哪些问题？"
          style={{ padding: '9px 12px', border: '1px solid var(--cv-border)', borderRadius: 8, background: 'var(--cv-surface)', color: 'var(--cv-ink)', font: 'inherit', resize: 'vertical' }}
        />
      </label>
      {error && <div className="cv-error" role="alert" style={{ marginTop: 8 }}><span>{error}</span></div>}
      <div style={{ display: 'flex', gap: 8, marginTop: 10, justifyContent: 'flex-end' }}>
        <button type="button" className="cv-btn cv-btn-secondary" onClick={onCancel} disabled={busy}>取消</button>
        <button type="submit" className="cv-btn cv-btn-primary" disabled={busy || !name.trim()}>
          {busy ? '保存中…' : '保存'}
        </button>
      </div>
    </form>
  )
}
