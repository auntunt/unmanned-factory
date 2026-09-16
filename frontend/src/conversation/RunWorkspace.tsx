import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { request } from '../workspace/api'
import type { ConversationMessage, Run } from '../workspace/types'
import { errorText, formatDate, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { useWorkTitle } from './title-context'
import { STAGES, HEAD_LABEL, TERMINAL_DONE, isTerminal, headState, stageIndex, composerMode } from './run-state'
import OperationResults from '../workbench/OperationResults'
import RequirementConfirmation from '../workbench/RequirementConfirmation'
import './conversation.css'

type Deliverable = { id: string | number; name: string; kind?: string; size?: number; preview?: boolean }
type DeliverList = { items?: Deliverable[]; saved?: boolean; collection_error?: string; recommended_preview_id?: string | number | null; can_collect?: boolean; repository_url?: string | null }
type LedgerItem = { id: string; text: string; status: string; evidence?: string }
type Ledger = { total?: number; counts?: { pass?: number; fail?: number; unverified?: number }; items?: LedgerItem[] }

function sizeLabel(bytes?: number): string {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export default function RunWorkspace({ csrfToken, onUnauthorized, user, pollMs = 5000 }: PageProps & { pollMs?: number }) {
  const { runId } = useParams()
  const navigate = useNavigate()
  const setWorkTitle = useWorkTitle()
  const [run, setRun] = useState<Run | null>(null)
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [queuedNote, setQueuedNote] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const seq = useRef(0)
  const sendLock = useRef(false)
  const submission = useRef({ signature: '', key: '' })

  const rid = runId ? encodeURIComponent(runId) : ''
  const load = useCallback(async () => {
    if (!rid) return
    const mine = ++seq.current
    try {
      const [r, c] = await Promise.all([
        request<Run>(`/api/v2/runs/${rid}`, { onUnauthorized }),
        request<{ messages: ConversationMessage[]; error?: string }>(`/api/v2/runs/${rid}/conversation`, { onUnauthorized }).catch(cause => ({ messages: null, error: errorText(cause) })),
      ])
      if (mine !== seq.current) return
      setRun(r); if (c.messages) setMessages(c.messages); setLoadError(c.error || null)
    } catch (cause) { if (mine === seq.current) setLoadError(errorText(cause)) }
  }, [rid, onUnauthorized])

  useEffect(() => {
    setRun(null); setMessages([]); setDraft(''); setLoadError(null); setActionError(null); setQueuedNote(null)
    void load()
    return () => { seq.current += 1 }
  }, [load])
  useEffect(() => {
    if (!run || isTerminal(run.status) || pollMs <= 0) return
    const timer = setInterval(() => { void load() }, pollMs)
    return () => clearInterval(timer)
  }, [run?.status, load, pollMs])
  useEffect(() => {
    const title = run?.plan?.title || (run ? `需求 #${run.id}` : null)
    setWorkTitle(title)
    return () => setWorkTitle(null)
  }, [run?.id, run?.plan?.title, setWorkTitle])
  useEffect(() => { const el = scrollRef.current; if (el) el.scrollTop = el.scrollHeight }, [messages.length, run?.status])

  const readOnly = run?.source?.type === 'inspection' || (run ? composerMode(run.status).kind === 'readonly' : true)
  const canAct = !readOnly && !run?.retry_run_id && (user?.role !== 'member' || String(run?.source?.actor_id) === String(user?.id))
  const mode = useMemo(() => (run ? composerMode(run.status) : null), [run])

  const dispatch = async (event: FormEvent) => {
    event.preventDefault()
    if (!run || !mode || sending || sendLock.current || !canAct) return
    const text = draft.trim()
    if (mode.needsText && !text) return
    sendLock.current = true
    const signature = JSON.stringify([rid, mode.kind, text])
    if (submission.current.signature !== signature) submission.current = { signature, key: crypto.randomUUID() }
    setSending(true); setActionError(null); setQueuedNote(null)
    const options = { method: 'POST', csrfToken, onUnauthorized } as const
    try {
      if (mode.kind === 'clarify') {
        await request(`/api/v2/runs/${rid}/clarify`, { ...options, body: { answer: text } })
      } else if (mode.kind === 'continue') {
        const analysisRetry = !run.plan && !run.spec_confirmation && run.source?.operation === 'general'
        if (analysisRetry && text) throw new Error('本次重试会沿用原始需求重新分析，补充文字尚未提交。请保留这段文字，清空输入后重试分析。')
        const resumable = analysisRetry || Boolean(run.artifacts?.base_sha && run.artifacts?.tasks)
        if (!resumable && text) {
          await request(`/api/v2/runs/${rid}/clarify`, { ...options, body: { answer: text } })
          setDraft(''); await load(); return
        }
        if (!resumable) {
          const next = await request<Run>(`/api/v3/runs/${rid}/retry`, options)
          navigate(`/runs/${encodeURIComponent(String(next.id))}`)
          return
        }
        await request(`/api/v2/runs/${rid}/continue`, { ...options, body: { answer: text, revision: run.revision, resume_count: run.resume_count ?? 0 } })
      } else if (mode.kind === 'approve') {
        // A typed adjustment re-opens planning (clarify); an empty send approves the plan.
        if (text) await request(`/api/v2/runs/${rid}/clarify`, { ...options, body: { answer: text } })
        else await request(`/api/v2/runs/${rid}/approve`, { ...options, body: { revision: run.revision } })
      } else if (mode.kind === 'confirm') {
        throw new Error('规格草案暂不可用，请刷新后通过规格表单确认。')
      } else if (mode.kind === 'followup') {
        const res = await request<{ recorded?: boolean; queued?: boolean; message?: string }>(`/api/v2/runs/${rid}/follow-up`, { ...options, body: { content: text, idempotency_key: submission.current.key } })
        setQueuedNote(res.message || '补充已记录，尚未执行；请在任务暂停或完成后重新提交需要落实的修改。')
      } else if (mode.kind === 'revise') {
        const project = run.project_id
        const created = await request<Run>('/api/v2/runs', { ...options, body: { project_id: project, request: `原始目标：${run.source?.original_request || run.root_request || run.request}\n\n本次修改要求：${text}`, operation: 'general', interaction_mode: 'automatic', idempotency_key: submission.current.key } })
        setDraft('')
        navigate(`/runs/${encodeURIComponent(String(created.id))}`)
        return
      }
      setDraft(''); submission.current = { signature: '', key: '' }
      await load()
    } catch (cause) { setActionError(errorText(cause)) } finally { sendLock.current = false; setSending(false) }
  }

  const stop = async () => {
    if (!run || sending || !canAct) return
    setSending(true); setActionError(null)
    try { await request(`/api/v2/runs/${rid}/cancel`, { method: 'POST', csrfToken, onUnauthorized, body: {} }); await load() }
    catch (cause) { setActionError(errorText(cause)) } finally { sendLock.current = false; setSending(false) }
  }

  if (loadError && !run) return <div className="cv-run"><div className="cv-loading" role="alert">{loadError}<button className="cv-btn" onClick={() => void load()}>重新读取</button></div></div>
  if (!run || String(run.id) !== runId) return <div className="cv-run"><div className="cv-loading"><span className="cv-spinner" />正在打开工作区…</div></div>

  const head = headState(run.status)
  const current = stageIndex(run.status)
  const title = run.plan?.title || `需求 #${run.id}`
  const ledger = (run.artifacts?.acceptance_ledger ?? null) as Ledger | null
  const failure = typeof run.error === 'string' ? run.error : typeof run.artifacts?.failure_reason === 'string' ? run.artifacts.failure_reason as string
    : head === 'fail' ? '本轮未能完成，已保留现有成果。' : null

  return (
    <div className="cv-run">
      <div className="cv-run-scroll" ref={scrollRef}>
        <div className="cv-run-inner">
          {loadError && <div className="cv-error" role="alert">刷新失败，保留上次记录：{loadError}</div>}
          {typeof run.source?.retry_of === 'string' && <p><Link to={`/runs/${encodeURIComponent(run.source.retry_of)}`}>查看上一次运行与证据</Link></p>}
          {run.retry_run_id && <p><Link to={`/runs/${encodeURIComponent(run.retry_run_id)}`}>查看接手此任务的后续运行</Link></p>}
          <section className="cv-progress" aria-label="任务进度">
            <div className="cv-progress-head">
              <span className={`cv-progress-dot ${head === 'active' ? 'is-active' : head === 'fail' ? 'is-fail' : head === 'wait' || head === 'paused' ? 'is-wait' : ''}`} />
              {run.status === 'inspection_completed' ? '巡检已完成' : String(run.status) === 'interrupted' ? '任务已中断' : HEAD_LABEL[head]}
            </div>
            <div className="cv-stages">
              {STAGES.map((s, index) => {
                const state = head === 'unknown' ? '' : head === 'done' && run.status !== 'inspection_completed' ? 'is-done' : head === 'fail' && index === current ? 'is-fail'
                  : index < current ? 'is-done' : index === current ? 'is-current' : ''
                return <div className={`cv-stage ${state}`} key={s.key}>
                  <span className="cv-stage-mark">{state === 'is-done' ? <Icon name="triangle" width={13} height={13} style={{ transform: 'rotate(0deg)' }} /> : null}</span>
                  {s.label}
                </div>
              })}
            </div>
          </section>

          <div className="cv-thread">
            {messages.length === 0 && <ThreadMessage message={{ id: 'request', role: 'user', content: String(run.source?.original_request || run.request), at: run.created_at }} />}
            {messages.map(message => <ThreadMessage key={String(message.id)} message={message} />)}

            {head === 'active' && <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div>
              <div className="cv-msg-body"><span className="cv-typing"><i /><i /><i /></span></div></div>}

            {(head === 'wait' || head === 'paused' || head === 'fail') && <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div>
              <div className="cv-msg-body">
                <div className={`cv-callout ${head === 'fail' ? 'is-fail' : 'is-wait'}`}>
                  <span className="cv-callout-mark"><Icon name={head === 'fail' ? 'preview' : 'preview'} width={18} height={18} /></span>
                  <div>
                    <strong>{head === 'fail' ? '这一轮没能完成' : head === 'paused' ? '任务已暂停' : '需要你补充'}</strong>
                    <p>{failure || [...(run.plan?.questions || []), ...(run.triage?.questions || [])].join('；') || '请在下面的输入框补充信息或材料后继续。已有成果已保留。'}</p>
                  </div>
                </div>
                <VerificationDrawer run={run} ledger={ledger} />
              </div></div>}

            {run.source?.operation && run.source.operation !== 'general' ? <OperationResults run={run} /> : null}
            {run.status === 'inspection_completed' && <p>巡检只诊断，未制作或发布产品。请查看本次检查结果。</p>}
            {head === 'unknown' && <p role="status">服务端状态：{run.status}。当前界面不支持此状态，未推断任务仍在执行。</p>}
            {TERMINAL_DONE.has(run.status) && <ProductCard key={rid} run={run} title={title} rid={rid} ledger={ledger} csrfToken={csrfToken} onUnauthorized={onUnauthorized} isAdmin={user?.role !== 'member'} />}
          </div>
        </div>
      </div>

      {readOnly ? <p className="cv-dock-note">{run.source?.type === 'inspection' ? '巡检仅记录诊断；如需修复，请提交独立维护任务。' : mode?.hint}</p> : run.status === 'awaiting_spec_confirmation' && run.spec_draft ? <div className="cv-run-inner"><RequirementConfirmation key={`${rid}:${run.revision}`} run={run} canAct={canAct} csrfToken={csrfToken} onUnauthorized={onUnauthorized} onChanged={next => { seq.current += 1; setRun(next); void load() }} /></div> : <div className="cv-dock">
        <div className="cv-dock-inner">
          <form onSubmit={dispatch}>
            <div className="cv-composer">
              <textarea rows={2} maxLength={mode?.kind === 'continue' ? 20000 : 50000} value={draft} disabled={sending || !canAct}
                onChange={e => setDraft(e.target.value)} placeholder={mode?.placeholder || '继续描述想调整的地方…'} aria-label="补充或修改"
                onKeyDown={e => { if (!e.shiftKey && e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} />
              <div className="cv-composer-foot">
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {mode?.approve && !draft.trim() && <button type="submit" className="cv-btn cv-btn-primary" disabled={sending}>{mode.approve}</button>}
                  {head === 'active' && <button type="button" className="cv-attach" disabled={sending} onClick={stop} aria-label="停止任务"><Icon name="preview" width={16} height={16} /> 停止</button>}
                </div>
                <button className="cv-send" type="submit" disabled={!canAct || sending || (mode?.needsText && !draft.trim())} aria-label={mode?.send || '发送'}>
                  {sending ? <span className="cv-spinner" style={{ borderTopColor: 'var(--cv-on-accent)' }} /> : <Icon name="arrow" width={20} height={20} />}
                </button>
              </div>
            </div>
          </form>
          {queuedNote && <p className="cv-queued-note">{queuedNote}</p>}
          {actionError && <div className="cv-error" role="alert"><span>{actionError}</span></div>}
          <p className="cv-dock-note">{!canAct ? '仅任务发起人或管理员可操作；后续运行请从上方链接打开。' : mode?.hint || '发送后会沿着当前任务继续。'}</p>
        </div>
      </div>}
    </div>
  )
}

function ThreadMessage({ message }: { message: ConversationMessage }) {
  const isUser = message.role === 'user'
  if (isUser) return <div className="cv-msg is-user"><div className="cv-msg-bubble">{message.content}{message.followup && message.applied === false && <small style={{ display: 'block' }}>仅已记录，尚未加入任务</small>}</div></div>
  return <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy<span style={{ marginLeft: 'auto', color: 'var(--cv-faint)', fontWeight: 400 }}>{formatDate(message.at)}</span></div>
    <div className="cv-msg-body">{message.content}</div></div>
}

function ProductCard({ run, title, rid, ledger, csrfToken, onUnauthorized, isAdmin }: { run: Run; title: string; rid: string; ledger: Ledger | null; csrfToken: string; onUnauthorized: () => void; isAdmin: boolean }) {
  const [deliver, setDeliver] = useState<DeliverList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [preview, setPreview] = useState<{ name: string; kind: string; content: string; image_url?: string } | null>(null)
  const base = `/api/v3/runs/${rid}/deliverables`
  useEffect(() => {
    const controller = new AbortController()
    request<DeliverList>(base, { onUnauthorized, signal: controller.signal }).then(value => { if (!controller.signal.aborted) { setDeliver(value); setError(null) } }).catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)) })
    return () => controller.abort()
  }, [base, onUnauthorized, refresh, run.updated_at])
  const items = deliver?.items || []
  const previewId = deliver?.recommended_preview_id ?? items.find(item => item.preview)?.id
  const openPreview = async () => {
    setBusy(true); setError(null)
    try { setPreview(await request(`${base}/files/${previewId}?preview=true`, { onUnauthorized })) }
    catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  const collect = async () => {
    setBusy(true); setError(null)
    try { await request(`${base}/collect`, { method: 'POST', csrfToken, onUnauthorized }); setRefresh(value => value + 1) }
    catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <div className="cv-msg is-assistant"><div className="cv-msg-head"><span className="cv-msg-avatar">w</span>webuddy</div><div className="cv-msg-body">
    {run.status === 'published' ? '本次任务已发布。' : '本次任务已完成，可查看成果与验证记录。'}
    <div className="cv-artifact" style={{ marginTop: 10 }}><div className="cv-artifact-head"><Icon name="delivery" width={17} height={17} />{title}</div><div className="cv-artifact-foot">
      {deliver?.saved && previewId != null && <button className="cv-btn cv-btn-primary" disabled={busy} onClick={() => void openPreview()}>预览成果</button>}
      {deliver?.saved && <a className="cv-btn cv-btn-secondary" href={`${base}/download`}><Icon name="download" width={15} height={15} />下载全部成果 ZIP</a>}
      {!deliver?.saved && deliver?.can_collect && isAdmin && <button className="cv-btn cv-btn-secondary" disabled={busy} onClick={() => void collect()}>保存成果后下载</button>}
    </div></div>
    {!deliver && !error && <p role="status">正在读取成果…</p>}
    {error && <div role="alert">{error}<button className="cv-btn" onClick={() => setRefresh(value => value + 1)}>重试读取成果</button></div>}
    {deliver && !deliver.saved && <p>{deliver.collection_error || (isAdmin ? '成果尚未归档。' : '请管理员保存成果后再下载。')}</p>}
    {preview && <section aria-label="成果预览"><h3>{preview.name}</h3><button className="cv-btn" onClick={() => setPreview(null)}>关闭预览</button>{preview.kind === 'image' && preview.image_url ? <img style={{ maxWidth: '100%' }} src={preview.image_url} alt={preview.name} /> : preview.kind === 'web' ? <><p>这是静态外观预览，脚本不运行；完整交互请下载并按说明启动。</p><iframe style={{ width: '100%', minHeight: 350 }} title={preview.name} sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:;">${preview.content}`} /></> : <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{preview.content}</pre>}</section>}
    {deliver?.saved && items.length > 0 && <details className="cv-collapse"><summary>全部文件 · {items.length}</summary>{items.map(item => <div className="cv-file" key={item.id}><div className="cv-file-meta"><strong>{item.name}</strong><small>{sizeLabel(item.size)}</small></div><a className="cv-btn cv-btn-secondary" href={`${base}/files/${item.id}`}>下载</a></div>)}</details>}
    <VerificationDrawer run={run} ledger={ledger} />
  </div></div>
}

function VerificationDrawer({ run, ledger }: { run: Run; ledger: Ledger | null }) {
  const c = ledger?.counts || {}
  const cost = run.artifacts?.total_known_cost_usd
  const commit = typeof run.artifacts?.commit === 'string' ? run.artifacts.commit as string : null
  const failure = typeof run.error === 'string' ? run.error : typeof run.artifacts?.failure_reason === 'string' ? run.artifacts.failure_reason as string : null
  const hasLedger = Boolean(ledger?.items?.length || ledger?.counts)
  if (!hasLedger && cost == null && !failure) return null
  return (
    <details className="cv-collapse">
      <summary><Icon name="triangle" className="cv-disclosure" width={13} height={13} /> 查看验证记录</summary>
      <div className="cv-collapse-body">
        {hasLedger && <div className="cv-verify-counts"><span>通过 {c.pass ?? 0}</span><span>未通过 {c.fail ?? 0}</span><span>未验证 {c.unverified ?? 0}</span>
          {typeof cost === 'number' && <span>费用 ${cost.toFixed(2)}</span>}</div>}
        {(ledger?.items || []).map(item => <div className="cv-verify-row" key={item.id}><span>{item.text}</span>
          <span className={`cv-verify-status ${item.status === 'pass' ? 'cv-verify-pass' : item.status === 'fail' ? 'cv-verify-fail' : 'cv-verify-unv'}`}>
            {item.status === 'pass' ? '通过' : item.status === 'fail' ? '未通过' : '未验证'}</span></div>)}
        {failure && <div className="cv-verify-row"><span>失败原因</span><span className="cv-verify-status cv-verify-fail">见下</span></div>}
        {failure && <p style={{ color: 'var(--cv-muted)', fontSize: 13, marginTop: 6 }}>{failure}</p>}
        {commit && <p style={{ color: 'var(--cv-faint)', fontSize: 12, marginTop: 8 }}>验收版本 {commit.slice(0, 8)} · 结果来自真实检查记录。</p>}
        {!commit && <p style={{ color: 'var(--cv-faint)', fontSize: 12, marginTop: 8 }}>结果来自真实检查记录。</p>}
      </div>
    </details>
  )
}
