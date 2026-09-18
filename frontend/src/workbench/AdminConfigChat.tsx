/**
 * Admin configuration chat panel for RuntimePage.
 *
 * Lets the admin configure runtime/operations/deploy-targets through
 * natural language, using the same backend tools as the form. The model
 * gets admin config tools only because the binding's admin_config flag
 * is determined server-side from the actor's actual role.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { request, WorkspaceApiError } from '../workspace/api'
import { errorText, type PageProps } from './ui'

type Msg = {
  id?: string
  role: string
  content: string
  status?: string
  job_id?: string
  created_at?: string
}

type ConfigConv = {
  id: string
  purpose: string
  actor_id: string | number
  messages: Msg[]
  created_at?: string
  updated_at?: string
}

const API = '/api/v4/admin-config'

interface AdminConfigChatProps extends PageProps {
  /** Called after the model finishes an answer so the form can re-read. */
  onConfigChanged?: () => void
}

export default function AdminConfigChat({ csrfToken, onUnauthorized, onConfigChanged }: AdminConfigChatProps) {
  const [conv, setConv] = useState<ConfigConv | null>(null)
  const [history, setHistory] = useState<ConfigConv[]>([])
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  // Load existing conversations on mount
  useEffect(() => {
    if (collapsed) return
    const c = new AbortController()
    request<{ conversations: ConfigConv[] }>(`${API}/conversations`, { signal: c.signal, onUnauthorized })
      .then(r => {
        setHistory(r.conversations || [])
        const latest = (r.conversations || [])[0]
        if (latest) {
          return request<ConfigConv>(`${API}/conversations/${encodeURIComponent(latest.id)}`, { signal: c.signal, onUnauthorized })
            .then(full => setConv(full))
        }
      })
      .catch(cause => {
        if (!c.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) {
          setError(errorText(cause))
        }
      })
    return () => c.abort()
  }, [collapsed, onUnauthorized])

  const messages = conv?.messages ?? []
  const pending = messages.some(m => m.status === 'pending' || m.status === 'running')

  const reload = useCallback(async () => {
    if (!conv?.id) return
    try {
      const fresh = await request<ConfigConv>(`${API}/conversations/${encodeURIComponent(conv.id)}`, { onUnauthorized })
      setConv(fresh)

      // Check if a pending message just completed — trigger config refresh
      const wasPending = messages.some(m => m.status === 'pending' || m.status === 'running')
      const nowPending = (fresh.messages ?? []).some(m => m.status === 'pending' || m.status === 'running')
      if (wasPending && !nowPending && onConfigChanged) {
        onConfigChanged()
      }
    } catch { /* keep last */ }
  }, [conv?.id, messages, onConfigChanged, onUnauthorized])

  // Poll while model is answering
  useEffect(() => {
    if (!pending) return
    const t = setInterval(() => { void reload() }, 2000)
    return () => clearInterval(t)
  }, [pending, reload])

  // Auto-scroll
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => { stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 60 }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [conv?.id])
  useEffect(() => {
    const el = scrollRef.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [messages.length, pending])

  const send = async (event: FormEvent) => {
    event.preventDefault()
    if (!text.trim() || sending) return
    setSending(true)
    setError(null)
    try {
      let current = conv
      if (!current) {
        current = await request<ConfigConv>(`${API}/conversations`, {
          method: 'POST', csrfToken, onUnauthorized, body: {},
        })
        setHistory(h => [current as ConfigConv, ...h])
      }
      const raw = await request<{ conversation?: ConfigConv; job_id?: string; status?: string; message?: string }>(
        `${API}/conversations/${encodeURIComponent(current.id)}/messages`,
        { method: 'POST', csrfToken, onUnauthorized, body: { content: text.trim() } },
      )
      if (raw.conversation) {
        setConv(raw.conversation)
        setHistory(h => [raw.conversation as ConfigConv, ...h.filter(x => x.id !== (raw.conversation as ConfigConv).id)])
      }
      setText('')
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setSending(false)
    }
  }

  const startNew = () => {
    setConv(null)
    setText('')
    setError(null)
  }

  const visible = messages.filter(m => !(m.status === 'pending' && m.job_id))

  if (collapsed) {
    return (
      <section className="wb-detail-card wb-runtime-section" aria-labelledby="config-chat-title">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h2 id="config-chat-title">配置对话</h2>
          <button className="wb-button" onClick={() => setCollapsed(false)}>展开</button>
        </div>
        <p className="wb-runtime-note">
          用自然语言配置运行环境、自动化和部署目标。和表单改的是同一份配置。
        </p>
      </section>
    )
  }

  return (
    <section className="wb-detail-card wb-runtime-section wb-config-chat" aria-labelledby="config-chat-title">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <h2 id="config-chat-title">配置对话</h2>
        <div style={{ display: 'flex', gap: 6 }}>
          <button className="wb-button" onClick={startNew}>新对话</button>
          <button className="wb-button" onClick={() => setCollapsed(true)}>收起</button>
        </div>
      </div>
      <p className="wb-runtime-note" style={{ margin: '0 0 10px' }}>
        用自然语言配置运行环境、自动化和部署目标。和表单改的是同一份配置。
      </p>

      {history.length > 1 && (
        <div className="wb-config-chat-history">
          {history.slice(0, 5).map(item => (
            <button
              key={item.id}
              className={`wb-button ${conv?.id === item.id ? 'wb-button-primary' : ''}`}
              style={{ fontSize: 12, padding: '4px 8px', minHeight: 28 }}
              onClick={() => {
                request<ConfigConv>(`${API}/conversations/${encodeURIComponent(item.id)}`, { onUnauthorized })
                  .then(setConv)
                  .catch(() => undefined)
              }}
            >
              {(item.messages?.find(m => m.role === 'user')?.content ?? '').slice(0, 16) || '空白'}
            </button>
          ))}
        </div>
      )}

      <div className="wb-config-chat-thread" ref={scrollRef}>
        {visible.length === 0 && !pending && (
          <div className="wb-config-chat-empty">
            <strong>配置助手</strong>
            <p>输入配置指令，例如「把 planner 模型改为 claude-sonnet-4-20250514」或「查看当前部署目标」。</p>
          </div>
        )}
        {visible.map(m => (
          <div className={`wb-config-msg ${m.role === 'user' ? 'is-user' : 'is-assistant'}`} key={m.id || m.created_at}>
            {m.role === 'user' ? (
              <div className="wb-config-msg-bubble">{m.content}</div>
            ) : (
              <div className="wb-config-msg-body">
                <span className="wb-config-msg-label">配置助手</span>
                <span className="wb-config-msg-content">{m.content}</span>
                {m.status === 'failed' && <span className="wb-config-msg-failed">回答失败</span>}
              </div>
            )}
          </div>
        ))}
        {pending && (
          <div className="wb-config-msg is-assistant">
            <div className="wb-config-msg-body">
              <span className="wb-config-msg-label">配置助手</span>
              <span className="wb-config-msg-content" style={{ color: 'var(--detail-muted)' }}>正在回答...</span>
            </div>
          </div>
        )}
      </div>

      <form onSubmit={send} className="wb-config-chat-form">
        <input
          type="text"
          value={text}
          disabled={sending}
          onChange={e => setText(e.target.value)}
          placeholder="输入配置指令..."
          aria-label="配置消息"
          style={{
            flex: 1,
            border: '1px solid var(--detail-border)',
            borderRadius: 7,
            padding: '8px 10px',
            background: 'var(--color-surface)',
            color: 'var(--detail-ink)',
            font: 'inherit',
            fontSize: 14,
          }}
        />
        <button
          className="wb-button wb-button-primary"
          type="submit"
          disabled={sending || !text.trim()}
          style={{ minHeight: 36 }}
        >
          {sending ? '发送中...' : '发送'}
        </button>
      </form>

      {error && <div className="wb-notice" role="alert" style={{ marginTop: 8, color: 'var(--color-danger)' }}>{error}</div>}
    </section>
  )
}
