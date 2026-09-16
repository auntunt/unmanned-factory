import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { request } from '../workspace/api'
import type { ConversationMessage, Run } from '../workspace/types'
import { errorText, formatDate, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { useWorkTitle } from './title-context'
import { STAGES, HEAD_LABEL, TERMINAL_DONE, isTerminal, headState, stageIndex, composerMode } from './run-state'
import './conversation.css'

type Deliverable = { id: string; name: string; kind?: string; size?: number; preview?: boolean }
type DeliverList = { items?: Deliverable[]; recommended_preview_id?: string | null; can_collect?: boolean; repository_url?: string | null }
type LedgerItem = { id: string; text: string; status: string; evidence?: string }
type Ledger = { total?: number; counts?: { pass?: number; fail?: number; unverified?: number }; items?: LedgerItem[] }

function sizeLabel(bytes?: number): string {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export default function RunWorkspace({ csrfToken, onUnauthorized, pollMs = 5000 }: PageProps & { pollMs?: number }) {
  const { runId } = useParams()
  const navigate = useNavigate()
  const setWorkTitle = useWorkTitle()
  const [run, setRun] = useState<Run | null>(null)
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [deliver, setDeliver] = useState<DeliverList | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [queuedNote, setQueuedNote] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const seq = useRef(0)

  const rid = runId ? encodeURIComponent(runId) : ''
  const load = useCallback(async () => {
    if (!rid) return
    const mine = ++seq.current
    try {
      const [r, c] = await Promise.all([
        request<Run>(`/api/v2/runs/${rid}`, { onUnauthorized }),
        request<{ messages: ConversationMessage[] }>(`/api/v2/runs/${rid}/conversation`, { onUnauthorized }).catch(() => ({ messages: [] as ConversationMessage[] })),
      ])
      if (mine !== seq.current) return
      setRun(r); setMessages(c.messages || []); setLoadError(null)
      if (TERMINAL_DONE.has(r.status)) {
        request<DeliverList>(`/api/v3/runs/${rid}/deliverables`, { onUnauthorized }).then(d => { if (mine === seq.current) setDeliver(d) }).catch(() => undefined)
      }
    } catch (cause) { if (mine === seq.current && !run) setLoadError(errorText(cause)) }
  }, [rid, onUnauthorized, run])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!run || isTerminal(run.status) || pollMs <= 0) return
    const timer = setInterval(() => { void load() }, pollMs)
    return () => clearInterval(timer)
  }, [run, load, pollMs])
  useEffect(() => {
    const title = run?.plan?.title || (run ? `需求 #${run.id}` : null)
    setWorkTitle(title)
    return () => setWorkTitle(null)
  }, [run, setWorkTitle])
  useEffect(() => { const el = scrollRef.current; if (el) el.scrollTop = el.scrollHeight }, [messages.length, run?.status])

  const mode = useMemo(() => (run ? composerMode(run.status) : null), [run])

  const dispatch = async (event: FormEvent) => {
    event.preventDefault()
    if (!run || !mode || sending) return
    const text = draft.trim()
    if (mode.needsText && !text) return
    setSending(true); setActionError(null); setQueuedNote(null)
    const options = { method: 'POST', csrfToken, onUnauthorized } as const
    try {
      if (mode.kind === 'clarify') {
        await request(`/api/v2/runs/${rid}/clarify`, { ...options, body: { answer: text } })
      } else if (mode.kind === 'continue') {
        await request(`/api/v2/runs/${rid}/continue`, { ...options, body: { answer: text, revision: run.revision, resume_count: run.resume_count ?? 0 } })
      } else if (mode.kind === 'approve') {
        // A typed adjustment re-opens planning (clarify); an empty send approves the plan.
        if (text) await request(`/api/v2/runs/${rid}/clarify`, { ...options, body: { answer: text } })
        else await request(`/api/v2/runs/${rid}/approve`, { ...options, body: { revision: run.revision } })
      } else if (mode.kind === 'confirm') {
        await request(`/api/v2/runs/${rid}/confirm-spec`, { ...options, body: { decision: 'proceed', revision: run.revision } })
      } else if (mode.kind === 'followup') {
        const res = await request<{ queued?: boolean; message?: string }>(`/api/v2/runs/${rid}/follow-up`, { ...options, body: { content: text } })
        if (res.queued) setQueuedNote(res.message || '补充需求已排队，当前步骤完成、需要你确认时会一并处理。')
      } else if (mode.kind === 'revise') {
        const project = run.project_id
        const created = await request<Run>('/api/v2/runs', { ...options, body: { project_id: project, request: text, operation: 'general', interaction_mode: 'automatic', idempotency_key: crypto.randomUUID() } })
        setDraft('')
        navigate(`/runs/${encodeURIComponent(String(created.id))}`)
        return
      }
      setDraft('')
      await load()
    } catch (cause) { setActionError(errorText(cause)) } finally { setSending(false) }
  }

  const stop = async () => {
    if (!run || sending) return
    setSending(true); setActionError(null)
    try { await request(`/api/v2/runs/${rid}/cancel`, { method: 'POST', csrfToken, onUnauthorized, body: {} }); await load() }
    catch (cause) { setActionError(errorText(cause)) } finally { setSending(false) }
  }

  if (loadError && !run) return <div className="cv-run"><div className="cv-loading">{loadError}</div></div>
  if (!run) return <div className="cv-run"><div className="cv-loading"><span className="cv-spinner" />正在打开工作区…</div></div>

  const head = headState(run.status)
  const current = stageIndex(run.status)
  const title = run.plan?.title || `需求 #${run.id}`
  const ledger = (run.artifacts?.acceptance_ledger ?? null) as Ledger | null
  const failure = typeof run.artifacts?.failure_reason === 'string' ? run.artifacts.failure_reason as string
    : head === 'fail' ? '本轮未能完成，已保留现有成果。' : null

  return (
    <div className="cv-run">
      <div className="cv-run-scroll" ref={scrollRef}>
        <div className="cv-run-inner">
          <section className="cv-progress" aria-label="任务进度">
            <div className="cv-progress-head">
              <span className={`cv-progress-dot ${head === 'active' ? 'is-active' : head === 'fail' ? 'is-fail' : head === 'wait' || head === 'paused' ? 'is-wait' : ''}`} />
              {HEAD_LABEL[head]}
            </div>
            <div className="cv-stages">
              {STAGES.map((s, index) => {
                const state = head === 'done' ? 'is-done' : head === 'fail' && index === current ? 'is-fail'
                  : index < current ? 'is-done' : index === current ? 'is-current' : ''
                return <div className={`cv-stage ${state}`} key={s.key}>
                  <span className="cv-stage-mark">{state === 'is-done' ? <Icon name="triangle" width={13} height={13} style={{ transform: 'rotate(0deg)' }} /> : null}</span>
                  {s.label}
                </div>
              })}
            </div>
          </section>

          <div className="cv-thread">
            {messages.map(message => <ThreadMessage key={String(message.id)} message={message} />)}

            {head === 'active' && <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div>
              <div className="cv-msg-body"><span className="cv-typing"><i /><i /><i /></span></div></div>}

            {(head === 'wait' || head === 'paused' || head === 'fail') && <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div>
              <div className="cv-msg-body">
                <div className={`cv-callout ${head === 'fail' ? 'is-fail' : 'is-wait'}`}>
                  <span className="cv-callout-mark"><Icon name={head === 'fail' ? 'preview' : 'preview'} width={18} height={18} /></span>
                  <div>
                    <strong>{head === 'fail' ? '这一轮没能完成' : head === 'paused' ? '任务已暂停' : '需要你补充'}</strong>
                    <p>{failure || run.plan?.questions?.[0] || '请在下面的输入框补充信息或材料后继续。已有成果已保留。'}</p>
                  </div>
                </div>
              </div></div>}

            {(head === 'done' || TERMINAL_DONE.has(run.status)) && <ProductCard run={run} title={title} deliver={deliver} rid={rid} ledger={ledger} />}
          </div>
        </div>
      </div>

      <div className="cv-dock">
        <div className="cv-dock-inner">
          <form onSubmit={dispatch}>
            <div className="cv-composer">
              <textarea rows={2} value={draft} disabled={sending}
                onChange={e => setDraft(e.target.value)} placeholder={mode?.placeholder || '继续描述想调整的地方…'} aria-label="补充或修改"
                onKeyDown={e => { if (!e.shiftKey && e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} />
              <div className="cv-composer-foot">
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {mode?.approve && !draft.trim() && <button type="submit" className="cv-btn cv-btn-primary" disabled={sending}>{mode.approve}</button>}
                  {head === 'active' && <button type="button" className="cv-attach" disabled={sending} onClick={stop} aria-label="停止任务"><Icon name="preview" width={16} height={16} /> 停止</button>}
                </div>
                <button className="cv-send" type="submit" disabled={sending || (mode?.needsText && !draft.trim())} aria-label={mode?.send || '发送'}>
                  {sending ? <span className="cv-spinner" style={{ borderTopColor: 'var(--cv-on-accent)' }} /> : <Icon name="arrow" width={20} height={20} />}
                </button>
              </div>
            </div>
          </form>
          {queuedNote && <p className="cv-queued-note">{queuedNote}</p>}
          {actionError && <div className="cv-error" role="alert"><span>{actionError}</span></div>}
          <p className="cv-dock-note">{mode?.hint || '发送后会沿着当前任务继续。'}</p>
        </div>
      </div>
    </div>
  )
}

function ThreadMessage({ message }: { message: ConversationMessage }) {
  const isUser = message.role === 'user'
  if (isUser) return <div className="cv-msg is-user"><div className="cv-msg-bubble">{message.content}</div></div>
  return <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy<span style={{ marginLeft: 'auto', color: 'var(--cv-faint)', fontWeight: 400 }}>{formatDate(message.at)}</span></div>
    <div className="cv-msg-body">{message.content}</div></div>
}

function ProductCard({ run, title, deliver, rid, ledger }: { run: Run; title: string; deliver: DeliverList | null; rid: string; ledger: Ledger | null }) {
  const items = deliver?.items || []
  const previewId = deliver?.recommended_preview_id || items.find(i => i.preview)?.id
  const zip = items.find(i => (i.kind || '').includes('source') || i.name.toLowerCase().endsWith('.zip'))
  const base = `/api/v3/runs/${rid}/deliverables`
  const published = run.status === 'published'
  return (
    <div className="cv-msg is-assistant">
      <div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div>
      <div className="cv-msg-body">
        {published ? '作品已发布，可直接使用。' : '作品已完成，验收通过。'}
        <div className="cv-artifact" style={{ marginTop: 10 }}>
          <div className="cv-artifact-head"><Icon name="delivery" width={17} height={17} />{title} · {published ? '已发布' : '当前版本'}</div>
          <div className="cv-artifact-foot">
            {previewId && <a className="cv-btn cv-btn-primary" href={`${base}/files/${previewId}?preview=true`} target="_blank" rel="noreferrer">打开作品 <Icon name="external" width={15} height={15} /></a>}
            {zip && <a className="cv-btn cv-btn-secondary" href={`${base}/files/${zip.id}`}><Icon name="download" width={15} height={15} /> 下载源码</a>}
            {!previewId && !zip && deliver?.can_collect && <a className="cv-btn cv-btn-secondary" href={`${base}/download`}><Icon name="download" width={15} height={15} /> 下载成果</a>}
          </div>
        </div>
        {zip && <div className="cv-file"><span className="cv-file-mark"><Icon name="delivery" width={17} height={17} /></span><div className="cv-file-meta"><strong>{zip.name}</strong><small>{sizeLabel(zip.size)}</small></div><a className="cv-btn cv-btn-secondary" href={`${base}/files/${zip.id}`}>下载</a></div>}
        {ledger && (ledger.items?.length || ledger.counts) && <VerificationDrawer ledger={ledger} />}
      </div>
    </div>
  )
}

function VerificationDrawer({ ledger }: { ledger: Ledger }) {
  const c = ledger.counts || {}
  return (
    <details className="cv-collapse">
      <summary><Icon name="triangle" className="cv-disclosure" width={13} height={13} /> 查看验证记录</summary>
      <div className="cv-collapse-body">
        <div className="cv-verify-counts"><span>通过 {c.pass ?? 0}</span><span>未通过 {c.fail ?? 0}</span><span>未验证 {c.unverified ?? 0}</span></div>
        {(ledger.items || []).map(item => <div className="cv-verify-row" key={item.id}><span>{item.text}</span>
          <span className={`cv-verify-status ${item.status === 'pass' ? 'cv-verify-pass' : item.status === 'fail' ? 'cv-verify-fail' : 'cv-verify-unv'}`}>
            {item.status === 'pass' ? '通过' : item.status === 'fail' ? '未通过' : '未验证'}</span></div>)}
        <p style={{ color: 'var(--cv-faint)', fontSize: 12, marginTop: 8 }}>验证结果来自真实检查记录。</p>
      </div>
    </details>
  )
}
