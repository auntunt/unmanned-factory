import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, formatDate, type PageProps } from '../workbench/ui'
import Icon, { CategoryBadge, packMark } from '../workbench/Icon'
import { unwrapAgentMessageResponse } from '../workbench/AgentsPage'
import CapabilityPanel from './CapabilityPanel'
import SessionSkillPanel from './SessionSkillPanel'
import { useWorkTitle } from './title-context'
import './conversation.css'

type Msg = { id?: string; role: string; content: string; at?: string; created_at?: string; status?: string; job_id?: string }
type Conv = { id: string; agent_id: string; mode: string; project_id?: string | null; messages: Msg[]; run_id?: string | null; updated_at?: string; attachments?: Array<{ id: string; name: string; size?: number }>; exports?: Array<{ id: string; title: string; format: string; size?: number }> }
type Agent = { id: string; name: string; purpose?: string; builtin_pack?: string; active_version?: number }
const base = '/api/v4'

/** 独立智能体日常聊天：复用无项目 do 会话（后台直接调模型、run=null）。
 *  只呈现消息、回答、产物——不显示工程六阶段/项目选择/代码验收/部署，也不建项目或开发运行。
 *  维护职能体在单独入口。 */
export default function AgentChatPage({ csrfToken, onUnauthorized }: PageProps) {
  const { agentId } = useParams()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const targetCid = params.get('cid')
  const setWorkTitle = useWorkTitle()
  const aid = agentId ? encodeURIComponent(agentId) : ''
  const [agent, setAgent] = useState<Agent | null>(null)
  const [conv, setConv] = useState<Conv | null>(null)
  const [history, setHistory] = useState<Conv[]>([])
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [attaching, setAttaching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const submitKey = useRef<string | null>(null)
  // One generation per selected route/conversation. Every async result (loader,
  // poll, send, attach) captures the generation it started in and drops itself if
  // the selection has since changed — a late result never mutates the new page.
  const gen = useRef(0)

  useEffect(() => {
    if (!aid) return
    const myGen = (gen.current += 1)
    const c = new AbortController()
    const live = () => gen.current === myGen
    setConv(null); setError(null)  // clear stale content before loading the new target
    request<Agent>(`${base}/agents/${aid}`, { signal: c.signal, onUnauthorized }).then(a => { if (live()) setAgent(a) }).catch(() => undefined)
    request<{ conversations?: Conv[] } | Conv[]>(`${base}/agents/${aid}/conversations`, { signal: c.signal, onUnauthorized })
      .then(r => {
        if (!live()) return
        const rows = (Array.isArray(r) ? r : r.conversations || []).filter(x => x.mode === 'do')
        setHistory(rows)
        if (targetCid === 'new') return  // an unsent new conversation: nothing to open
        // Open the conversation named in the URL (?cid=…) so a refresh or a second
        // tab restores that exact target, not just the role's latest chat. The GET
        // enforces ownership (403 for another user's conversation). Fall back to the
        // most recent only when no cid is pinned.
        const open = targetCid || rows[0]?.id
        if (!open) return
        return request<Conv>(`${base}/conversations/${encodeURIComponent(open)}`, { signal: c.signal, onUnauthorized }).then(loaded => {
          if (!live()) return  // a newer selection superseded this request
          // The pinned conversation must belong to THIS role and be a chat, not a
          // maintenance session or another role's conversation shown under this title.
          if (loaded.agent_id !== agentId || loaded.mode !== 'do') {
            setConv(null); setError('这个会话不属于当前助手，或不是日常对话。')
            return
          }
          setConv(loaded)
        })
      })
      .catch(cause => { if (live() && !c.signal.aborted) setError(errorText(cause)) })
    return () => c.abort()
  }, [aid, agentId, targetCid, onUnauthorized])

  useEffect(() => { setWorkTitle(agent?.name || null); return () => setWorkTitle(null) }, [agent?.name, setWorkTitle])

  const messages = conv?.messages ?? []
  const pending = messages.some(m => m.status === 'pending' || m.status === 'running')

  const reload = useCallback(async () => {
    if (!conv?.id) return
    const myGen = gen.current
    try { const fresh = await request<Conv>(`${base}/conversations/${encodeURIComponent(conv.id)}`, { onUnauthorized }); if (gen.current === myGen) setConv(fresh) } catch { /* keep last */ }
  }, [conv?.id, onUnauthorized])

  // Poll only while the model is still answering (async job); stop when settled.
  useEffect(() => {
    if (!pending) return
    const t = setInterval(() => { void reload() }, 2000)
    return () => clearInterval(t)
  }, [pending, reload])

  // Follow new messages in the shell's scroll container; don't yank when reading up.
  const scroller = useCallback(() => (scrollRef.current?.closest('.as-content') as HTMLElement | null) ?? null, [])
  useEffect(() => {
    const el = scroller(); if (!el) return
    const onScroll = () => { stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 80 }
    el.addEventListener('scroll', onScroll, { passive: true }); return () => el.removeEventListener('scroll', onScroll)
  }, [scroller, conv?.id])
  useEffect(() => { const el = scroller(); if (el && stick.current) el.scrollTop = el.scrollHeight }, [messages.length, pending, scroller])

  const send = async (event: FormEvent) => {
    event.preventDefault()
    // Mutually exclusive with attach: both can create a conversation, so running them
    // together could fork two conversations.
    if (!text.trim() || sending || attaching || !aid) return
    const myGen = gen.current
    setSending(true); setError(null)
    // A stable key per composed message: reused on retry so a duplicate submit is
    // recognised server-side and never re-answered; reset only after it lands.
    if (!submitKey.current) submitKey.current = crypto.randomUUID()
    try {
      let current = conv
      if (!current) {
        // Idempotent create: a lost response on retry returns the same conversation
        // (server keys on this op), never a duplicate.
        current = await request<Conv>(`${base}/agents/${aid}/conversations`, { method: 'POST', csrfToken, onUnauthorized, body: { mode: 'do', project_id: null, client_key: submitKey.current } })
        if (gen.current === myGen) setHistory(h => [current as Conv, ...h])
      }
      const raw = await request<{ conversation?: Conv; run?: unknown; needs_project?: boolean; busy?: boolean; message?: string }>(`${base}/conversations/${encodeURIComponent(current.id)}/messages`, { method: 'POST', csrfToken, onUnauthorized, body: { content: text.trim(), idempotency_key: submitKey.current } })
      // The user has since switched conversation/role: the server action stands, but a
      // stale result must not overwrite the new page, its draft or its navigation.
      if (gen.current !== myGen) return
      if (raw.conversation) { setConv(raw.conversation); setHistory(h => [raw.conversation as Conv, ...h.filter(x => x.id !== (raw.conversation as Conv).id)]) }
      if (raw.busy) { setError(raw.message || '上一条还在回答，请稍候再发送。'); return }  // keep draft + key
      const res = unwrapAgentMessageResponse(raw as never)
      if (res.needsProject) setError('这个职能体还没有配置可用模型，暂时无法回答。请在维护里配置模型后再聊。')
      submitKey.current = null; setText('')
      if (current.id !== targetCid) navigate(`/agents/${aid}/chat?cid=${encodeURIComponent(current.id)}`, { replace: true }) // pin the new conversation
    } catch (cause) { if (gen.current === myGen) setError(errorText(cause)) } finally { setSending(false) }
  }

  const attach = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = ''
    if (!file || !aid || attaching || sending) return  // exclusive with send
    const myGen = gen.current
    setAttaching(true); setError(null)
    if (!submitKey.current) submitKey.current = crypto.randomUUID()
    try {
      let current = conv
      if (!current) { current = await request<Conv>(`${base}/agents/${aid}/conversations`, { method: 'POST', csrfToken, onUnauthorized, body: { mode: 'do', project_id: null, client_key: submitKey.current } }); if (gen.current === myGen) setHistory(h => [current as Conv, ...h]) }
      const body = new FormData(); body.append('file', file)
      const res = await request<{ conversation: Conv }>(`${base}/conversations/${encodeURIComponent(current.id)}/attachments`, { method: 'POST', csrfToken, onUnauthorized, body })
      if (gen.current !== myGen) return  // switched away: keep the new page intact
      if (res.conversation) setConv(res.conversation)
      if (current.id !== targetCid) navigate(`/agents/${aid}/chat?cid=${encodeURIComponent(current.id)}`, { replace: true })
    } catch (cause) { if (gen.current === myGen) setError(errorText(cause)) } finally { setAttaching(false) }
  }

  // History and new-chat switches go through the URL so refresh and Back stay on the
  // chosen conversation; the loader effect (keyed on ?cid=) does the fetch + checks.
  const startNew = () => { setText(''); setError(null); submitKey.current = null; navigate(`/agents/${aid}/chat?cid=new`) }
  const openConv = (id: string) => { setText(''); submitKey.current = null; navigate(`/agents/${aid}/chat?cid=${encodeURIComponent(id)}`) }

  const mark = useMemo(() => (agent ? packMark(agent) : null), [agent])
  const visible = messages.filter(m => !(m.status === 'pending' && m.job_id))

  return (
    <div className="cv-chat">
      <div className="cv-chat-head">
        <CategoryBadge avatar={Array.from(agent?.name || '·')[0]} mark={mark} />
        <div><h1>{agent?.name || '职能体'}</h1><p>{agent?.purpose || '日常聊天：提问、让它按已配置的能力回答。'}</p></div>
        <div className="cv-chat-actions">
          <button className="cv-btn cv-btn-secondary" onClick={startNew}><Icon name="plus" width={15} height={15} /> 新对话</button>
          <Link className="cv-btn cv-btn-secondary" to={`/agents/${aid}?mode=maintain`}>维护方法</Link>
        </div>
      </div>
      {history.length > 0 && <div className="cv-chat-history">
        {history.slice(0, 6).map(item => <button key={item.id} className={conv?.id === item.id ? 'is-active' : ''} onClick={() => void openConv(item.id)} title={item.messages?.find(m => m.role === 'user')?.content}>
          {item.messages?.find(m => m.role === 'user')?.content?.slice(0, 24) || '空白对话'}</button>)}
      </div>}
      <div className="cv-chat-thread" ref={scrollRef}>
        {visible.length === 0 && !pending && <div className="cv-chat-empty"><strong>和「{agent?.name || '这个职能体'}」聊点什么？</strong><p>它会按已配置的能力回答；需要开发软件时再明确说，会转交开发流程并保留这段对话。</p></div>}
        {visible.map(m => m.role === 'user'
          ? <div className="cv-msg is-user" key={m.id || m.at}><div className="cv-msg-bubble">{m.content}</div></div>
          : <div className="cv-msg is-assistant" key={m.id || m.at}><div className="cv-msg-head"><span className="cv-msg-avatar">{Array.from(agent?.name || 'w')[0]}</span>{agent?.name || 'webuddy'}<span style={{ marginLeft: 'auto', color: 'var(--cv-faint)', fontWeight: 400 }}>{formatDate(m.at || m.created_at)}</span></div><div className="cv-msg-body">{m.content}</div></div>)}
        {pending && <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">{Array.from(agent?.name || 'w')[0]}</span>{agent?.name || 'webuddy'}</div><div className="cv-msg-body"><span className="cv-typing"><i /><i /><i /></span></div></div>}
      </div>
      {(conv?.exports?.length ?? 0) > 0 && <div className="cv-chat-history" style={{ marginBottom: 8 }}>
        {conv!.exports!.map(e => <a key={e.id} className="cv-filechip" style={{ textDecoration: 'none' }} href={`/api/v4/conversations/${encodeURIComponent(conv!.id)}/exports/${encodeURIComponent(e.id)}/download`}>
          <Icon name="download" width={13} height={13} /><span title={e.title}>{e.title}.{e.format}</span></a>)}
      </div>}
      <div className="cv-dock">
        <div className="cv-dock-inner">
          {agentId && <CapabilityPanel agentId={agentId} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}
          {conv?.id && <SessionSkillPanel sessionId={conv.id} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}
          <form onSubmit={send}>
            <div className="cv-composer">
              <textarea rows={2} value={text} disabled={sending} onChange={e => { setText(e.target.value); submitKey.current = null }} placeholder={`和 ${agent?.name || '职能体'} 聊…`} aria-label="消息"
                onKeyDown={e => { if (!e.shiftKey && e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} />
              {(conv?.attachments?.length ?? 0) > 0 && <div className="cv-filechips">{conv!.attachments!.map(a => <span className="cv-filechip" key={a.id}><Icon name="delivery" width={13} height={13} /><span title={a.name}>{a.name}</span></span>)}</div>}
              <div className="cv-composer-foot">
                <label className="cv-attach" title="仅支持 UTF-8 文本：.txt / .md / .csv，单个 ≤40KB（暂不支持 PDF / Word / Excel）"><Icon name="delivery" width={16} height={16} /> {attaching ? '上传中…' : '添加材料'}<input type="file" accept=".txt,.md,.csv,text/plain" disabled={attaching || sending} onChange={attach} /></label>
                <button className="cv-send" type="submit" disabled={sending || attaching || !text.trim()} aria-label="发送">{sending ? <span className="cv-spinner" style={{ borderTopColor: 'var(--cv-on-accent)' }} /> : <Icon name="arrow" width={20} height={20} />}</button>
              </div>
            </div>
          </form>
          {error && <div className="cv-error" role="alert"><span>{error}</span></div>}
          <p className="cv-dock-note">日常聊天不会自动建项目或开发运行。</p>
        </div>
      </div>
    </div>
  )
}
