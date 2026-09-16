import { useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { validateProjectFiles, validateProjectZip, type ProjectRecord } from '../workbench/ProjectsPage'
import { readOp, writeOp, clearOp, readDraft, writeDraft, clearDraft, hashFile, type OpAttachment } from './entry-recovery'

/** The single daily entry: one Chatbox. Describe a goal, optionally attach
 *  material, send. The server decides the path (development / a standalone role's
 *  chat / ask-one-question / not-available); the client never asserts the route.
 *  Development creates a project + run; role chat opens a no-project conversation
 *  (no run); ambiguity asks instead of assuming code. One operation key (per tab,
 *  per actor) makes every step idempotent, so a lost response on retry resumes the
 *  same conversation/project instead of duplicating it. */
type Decision = {
  kind: 'development' | 'agent_chat' | 'clarify' | 'unavailable'
  agent_id?: string; agent_name?: string; reason?: string; mode_label?: string
  question?: string; roles?: Array<{ agent_id: string; name: string }>
  offer_development?: boolean; missing?: string[]; detail?: string; attach?: boolean
}

export default function StartChat({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const uid = user?.id
  const [goal, setGoal] = useState(() => readDraft(uid)) // per-actor, per-tab draft
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<Decision | null>(null)
  const [focused, setFocused] = useState(false)
  const attempt = useRef<{ project?: ProjectRecord }>({})
  const lock = useRef(false)
  // True once the user changes the file selection in THIS mount. Distinguishes an
  // explicit remove/replace (authoritative) from a refresh that merely lost the File
  // objects (recover the saved materials, ask to re-select — don't drop them).
  const filesTouched = useRef(false)
  // Stable per-submission id, resumed from this tab's saved state when the (trimmed)
  // goal is unchanged, so a refresh continues the SAME operation and key rather than
  // starting over — even if the very first create response was lost.
  const opRef = useRef<string>('')
  const opGoalRef = useRef<string>('')
  if (!opRef.current) {
    const saved = readOp(uid)
    const g = goal.trim()
    if (saved && saved.goal === g) {
      opRef.current = saved.op
      if (saved.kind === 'dev' && saved.projectId) attempt.current.project = { id: saved.projectId } as ProjectRecord
    } else {
      opRef.current = crypto.randomUUID()
    }
    opGoalRef.current = g
  }

  const options = () => ({ method: 'POST' as const, csrfToken, onUnauthorized })
  // A new goal is a new submission: rotate the op key and drop any resumable state.
  // Material changes are NOT handled here — re-selecting the same content must keep
  // the op; a genuine material change is detected by content hash at submit time.
  const onGoalChange = (value: string) => {
    setGoal(value); setDecision(null); writeDraft(uid, value)
    const g = value.trim()
    if (g !== opGoalRef.current) { opRef.current = crypto.randomUUID(); opGoalRef.current = g; attempt.current = {}; clearOp(uid) }
  }

  const runDevelopment = async (resume: boolean) => {
    const g = goal.trim(); const op = opRef.current
    const saved = resume ? readOp(uid) : null
    // Input mode is fixed when the task starts and never switched on recovery:
    // a task begun as an import must never fall back to an empty create-workspace.
    const archive = files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')
    const mode: 'workspace' | 'import-files' | 'import-zip' =
      resume && saved?.mode ? saved.mode : files.length ? (archive ? 'import-zip' : 'import-files') : 'workspace'
    const manifest: OpAttachment[] = resume && saved?.manifest ? saved.manifest
      : files.map(f => ({ name: f.name, hash: '', size: f.size })) // dev: name+size metadata (server verifies content)
    let projectId = resume ? saved?.projectId : attempt.current.project ? String(attempt.current.project.id) : undefined
    // Persist mode + manifest BEFORE any side-effect, so a lost import response +
    // refresh restores the same op/mode/materials rather than a blank workspace.
    writeOp(uid, { op, kind: 'dev', goal: g, mode, manifest, projectId })

    if (!projectId) {
      if (mode !== 'workspace') {
        // The project needs materials but the File objects are gone (refresh) or were
        // never re-provided: require the same materials again; never create an empty one.
        const covered = files.length > 0 && manifest.every(m => files.some(f => f.name === m.name && f.size === m.size))
        if (!covered) { setError(`材料未恢复，请重新选择后再开始：${manifest.map(m => m.name).join('、')}`); return }
      }
      setStage(mode === 'workspace' ? '正在准备工作区…' : '正在准备资料…')
      const name = g.replace(/\s+/g, ' ').slice(0, 60)
      if (mode === 'workspace') {
        attempt.current.project = await request<ProjectRecord>('/api/v2/projects/create-workspace', { ...options(), body: { name, idempotency_key: op } })
      } else {
        const body = new FormData()
        if (mode === 'import-zip') body.append('file', files[0]); else files.forEach(file => body.append('files', file))
        body.append('name', name); body.append('idempotency_key', op)
        const result = await request<{ project: ProjectRecord }>(mode === 'import-zip' ? '/api/v2/projects/import-zip' : '/api/v2/projects/import-files', { ...options(), body })
        attempt.current.project = result.project
      }
      projectId = String(attempt.current.project.id)
      writeOp(uid, { op, kind: 'dev', goal: g, mode, manifest, projectId })
    } else {
      attempt.current.project = { id: projectId } as ProjectRecord
    }
    setStage('正在开始制作…')
    const run = await request<Run>('/api/v2/runs', { ...options(), body: { project_id: projectId, request: g, operation: 'general', interaction_mode: 'automatic', idempotency_key: op } })
    clearOp(uid); clearDraft(uid)
    navigate(`/runs/${encodeURIComponent(String(run.id))}`)
  }

  const openAgentChat = async (agentId: string, attach?: boolean) => {
    const g = goal.trim()
    // Content-hash the current material; a same-named edited file is different, and a
    // material lost to a refresh is detectable.
    const current = attach ? await Promise.all(files.map(async f => ({ file: f, name: f.name, hash: await hashFile(f), size: f.size }))) : []
    const currentHashes = current.map(c => c.hash).slice().sort()
    let saved = readOp(uid)
    if (saved && (saved.kind !== 'chat' || saved.goal !== g || saved.agentId !== agentId)) saved = null
    // Only an EXPLICIT change to the selection (user added/removed/replaced files this
    // mount) is a genuine material change -> new op/conversation, honouring a removal.
    // A refresh that merely lost the File objects (untouched, empty) keeps the saved op
    // so the missing-material guard below asks for the same content back — it never
    // silently resends removed material nor drops recorded material.
    if (saved && filesTouched.current) {
      const savedHashes = (saved.manifest || []).map(m => m.hash).slice().sort()
      if (JSON.stringify(savedHashes) !== JSON.stringify(currentHashes)) { saved = null; opRef.current = crypto.randomUUID(); clearOp(uid) }
    }
    const op = saved?.op || opRef.current
    opRef.current = op; opGoalRef.current = g
    const manifest: OpAttachment[] = saved?.manifest ?? current.map(({ name, hash, size }) => ({ name, hash, size }))
    const uploaded: string[] = saved?.uploaded ? [...saved.uploaded] : []
    // A required material that is neither uploaded nor present now (File lost on a
    // refresh) blocks the submit — never send the text without it.
    const missing = manifest.filter(m => !uploaded.includes(m.hash) && !current.some(c => c.hash === m.hash))
    if (missing.length) { setError(`材料未恢复，请重新选择后再发送：${missing.map(m => m.name).join('、')}`); return }

    let cid = saved?.cid
    const persist = () => writeOp(uid, { op, kind: 'chat', goal: g, agentId, cid, manifest, uploaded })
    persist() // BEFORE the first side-effect: a lost create response + refresh restores this op/key
    setStage('正在打开对话…')
    if (!cid) {
      const conv = await request<{ id: string }>(`/api/v4/agents/${encodeURIComponent(agentId)}/conversations`, { ...options(), body: { mode: 'do', project_id: null, client_key: op } })
      cid = conv.id; persist()
    }
    for (const entry of manifest) {
      if (uploaded.includes(entry.hash)) continue
      const found = current.find(c => c.hash === entry.hash)!
      const body = new FormData(); body.append('file', found.file)
      await request(`/api/v4/conversations/${encodeURIComponent(cid)}/attachments`, { ...options(), body }) // idempotent by content
      uploaded.push(entry.hash); persist()
    }
    await request(`/api/v4/conversations/${encodeURIComponent(cid)}/messages`, { ...options(), body: { content: g, idempotency_key: op } })
    clearOp(uid); clearDraft(uid)
    navigate(`/agents/${encodeURIComponent(agentId)}/chat?cid=${encodeURIComponent(cid)}`)
  }

  const submit = async (event?: FormEvent, override?: { agentId?: string; forceDev?: boolean }) => {
    event?.preventDefault?.()
    if (lock.current || !goal.trim()) return
    const archive = files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')
    const problem = files.length ? archive ? validateProjectZip(files[0]) : validateProjectFiles(files) : null
    if (problem) { setError(problem); return }
    lock.current = true; setBusy(true); setError(null); setDecision(null)
    try {
      const g = goal.trim()
      // A forced development choice from the clarify panel.
      if (override?.forceDev) { await runDevelopment(false); return }
      // Resume a saved in-flight operation for THIS goal instead of re-routing: a
      // refresh must continue the established task (its kind, agent and input mode),
      // never re-run routing with a now-empty file list and change the decision. An
      // explicit role pick (override.agentId) still goes through the server so the
      // chosen role is re-validated.
      if (!override) {
        const saved = readOp(uid)
        if (saved && saved.goal === g) {
          if (saved.kind === 'dev') { await runDevelopment(true); return }
          if (saved.kind === 'chat' && saved.agentId) { await openAgentChat(saved.agentId, (saved.manifest?.length ?? 0) > 0 || files.length > 0); return }
        }
      }
      // A new task (or an explicit role pick): let the server decide/validate the path.
      setStage('正在判断处理方式…')
      const attachments = files.map(file => ({ name: file.name, size: file.size, type: file.type }))
      const route = await request<Decision>('/api/v4/route', { ...options(), body: { text: g, agent_id: override?.agentId ?? null, attachments } })
      if (route.kind === 'development') { await runDevelopment(false); return }
      if (route.kind === 'agent_chat' && route.agent_id) { await openAgentChat(route.agent_id, route.attach); return }
      setDecision(route) // clarify | unavailable — shown inline, goal + materials kept
    } catch (cause) { setError(errorText(cause)) } finally { lock.current = false; setBusy(false) }
  }

  if (user?.role === 'member') {
    return <div className="cv-start"><h1>开始制作</h1><p className="cv-sub">打开已分配的作品，直接描述你想完成的事。</p><Link className="cv-btn cv-btn-primary" to="/projects">我的作品</Link></div>
  }

  const locked = busy || Boolean(attempt.current.project)
  return (
    <div className="cv-start">
      <h1>你想做什么？</h1>
      <p className="cv-sub">说清目标，剩下的交给我。开发、报价、会议总结都可以直接说。</p>
      <form onSubmit={submit} onKeyDown={event => { if (!event.shiftKey && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.requestSubmit() } }}>
        <div className={`cv-composer${focused ? ' is-focused' : ''}`}>
          <textarea required maxLength={50000} rows={3} disabled={locked}
            value={goal} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
            onChange={event => onGoalChange(event.target.value)}
            placeholder="描述你想做的产品，或让某个助手处理日常事务…" aria-label="需求" />
          {files.length > 0 && (
            <div className="cv-filechips">
              {files.map((file, index) => (
                <span className="cv-filechip" key={`${file.name}-${index}`}><span title={file.name}>{file.name}</span>
                  <button type="button" aria-label={`移除 ${file.name}`} disabled={locked} onClick={() => { filesTouched.current = true; setFiles(current => current.filter((_, i) => i !== index)) }}><Icon name="plus" style={{ transform: 'rotate(45deg)' }} width={14} height={14} /></button></span>
              ))}
            </div>
          )}
          <div className="cv-composer-foot">
            <label className="cv-attach"><Icon name="delivery" width={17} height={17} /> 添加材料
              <input type="file" multiple disabled={locked} onChange={event => { filesTouched.current = true; setFiles(Array.from(event.target.files || [])); setError(null) }} /></label>
            <button className="cv-send" type="submit" disabled={busy || !goal.trim()} aria-label="开始制作">
              {busy ? <span className="cv-spinner" style={{ borderTopColor: 'var(--cv-on-accent)' }} /> : <Icon name="arrow" width={20} height={20} />}
            </button>
          </div>
        </div>
      </form>
      {busy && <p className="cv-hint">{stage}</p>}
      {!busy && !decision && <p className="cv-hint">支持 ZIP、文档与样例，项目材料最大 1 GB。只有缺少必要信息时才会问你。</p>}

      {decision?.kind === 'clarify' && (
        <div className="cv-route" role="group" aria-label="选择处理方式">
          <p className="cv-route-q">{decision.question || '想怎么处理？'}</p>
          <div className="cv-route-actions">
            {(decision.roles || []).map(role => (
              <button key={role.agent_id} type="button" className="cv-btn cv-btn-secondary" disabled={busy}
                onClick={() => void submit(undefined, { agentId: role.agent_id })}>交给「{role.name}」</button>
            ))}
            {decision.offer_development && (
              <button type="button" className="cv-btn cv-btn-secondary" disabled={busy}
                onClick={() => void submit(undefined, { forceDev: true })}>按开发处理</button>
            )}
          </div>
        </div>
      )}
      {decision?.kind === 'unavailable' && (
        <div className="cv-route cv-route-blocked" role="status">
          <p className="cv-route-q"><strong>暂不可用</strong>：{decision.reason}</p>
          {decision.missing?.length ? <ul className="cv-route-missing">{decision.missing.map((m, i) => <li key={i}>{m}</li>)}</ul> : null}
          <p className="cv-hint">你可以修改需求后重试，或换一件事。</p>
        </div>
      )}

      {error && <div className="cv-error" role="alert"><strong>没有开始成功</strong><span>{error}</span>{attempt.current.project && <Link className="cv-verify-link" to={`/projects/${encodeURIComponent(String(attempt.current.project.id))}`}>工作区已保留，可打开查看</Link>}</div>}
      <p className="cv-example">例如：<b>做一个能预约、改期和导出记录的管理工具</b>，或 <b>帮客户算一批设备的报价</b></p>
    </div>
  )
}
