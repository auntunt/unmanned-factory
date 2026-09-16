import { useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import { validateProjectFiles, validateProjectZip, type ProjectRecord } from '../workbench/ProjectsPage'

/** The single daily entry: one Chatbox. Describe a goal, optionally attach
 *  material, send. Project creation + run submission happen behind the scenes
 *  (each idempotent), then we move into the run's conversation workspace.
 *  The create contract mirrors StartPage exactly. */
export default function StartChat({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const [goal, setGoal] = useState(() => { try { return localStorage.getItem('webuddy:start:goal') || '' } catch { return '' } })
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [focused, setFocused] = useState(false)
  const attempt = useRef<{ key: string; project?: ProjectRecord; runKey: string }>({ key: crypto.randomUUID(), runKey: crypto.randomUUID() })
  const lock = useRef(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (lock.current || !goal.trim()) return
    const archive = files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')
    const problem = files.length ? archive ? validateProjectZip(files[0]) : validateProjectFiles(files) : null
    if (problem) { setError(problem); return }
    lock.current = true; setBusy(true); setError(null)
    try {
      const options = { method: 'POST', csrfToken, onUnauthorized }
      if (!attempt.current.project) {
        setStage(files.length ? '正在准备资料…' : '正在准备工作区…')
        const name = goal.trim().replace(/\s+/g, ' ').slice(0, 60)
        if (files.length) {
          const body = new FormData()
          if (archive) body.append('file', files[0]); else files.forEach(file => body.append('files', file))
          body.append('name', name); body.append('idempotency_key', attempt.current.key)
          const result = await request<{ project: ProjectRecord }>(archive ? '/api/v2/projects/import-zip' : '/api/v2/projects/import-files', { ...options, body })
          attempt.current.project = result.project
        } else {
          attempt.current.project = await request<ProjectRecord>('/api/v2/projects/create-workspace', { ...options, body: { name, idempotency_key: attempt.current.key } })
        }
      }
      setStage('正在开始制作…')
      const run = await request<Run>('/api/v2/runs', { ...options, body: { project_id: attempt.current.project.id, request: goal.trim(), operation: 'general', interaction_mode: 'automatic', idempotency_key: attempt.current.runKey } })
      try { localStorage.removeItem('webuddy:start:goal') } catch { /* Storage is optional. */ }
      navigate(`/runs/${encodeURIComponent(String(run.id))}`)
    } catch (cause) { setError(errorText(cause)) } finally { lock.current = false; setBusy(false) }
  }

  if (user?.role === 'member') {
    return <div className="cv-start"><h1>开始制作</h1><p className="cv-sub">打开已分配的作品，直接描述你想完成的事。</p><Link className="cv-btn cv-btn-primary" to="/projects">我的作品</Link></div>
  }

  const locked = busy || Boolean(attempt.current.project)
  return (
    <div className="cv-start">
      <h1>你想做什么？</h1>
      <p className="cv-sub">说清目标，剩下的交给我。</p>
      <form onSubmit={submit} onKeyDown={event => { if (!event.shiftKey && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.requestSubmit() } }}>
        <div className={`cv-composer${focused ? ' is-focused' : ''}`}>
          <textarea required maxLength={50000} rows={3} disabled={locked}
            value={goal} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
            onChange={event => { setGoal(event.target.value); try { localStorage.setItem('webuddy:start:goal', event.target.value) } catch { /* optional */ } }}
            placeholder="描述你想做的产品，也可以直接添加现有项目…" aria-label="需求" />
          {files.length > 0 && (
            <div className="cv-filechips">
              {files.map((file, index) => (
                <span className="cv-filechip" key={`${file.name}-${index}`}><span title={file.name}>{file.name}</span>
                  <button type="button" aria-label={`移除 ${file.name}`} disabled={locked} onClick={() => setFiles(current => current.filter((_, i) => i !== index))}><Icon name="plus" style={{ transform: 'rotate(45deg)' }} width={14} height={14} /></button></span>
              ))}
            </div>
          )}
          <div className="cv-composer-foot">
            <label className="cv-attach"><Icon name="delivery" width={17} height={17} /> 添加材料
              <input type="file" multiple disabled={locked} onChange={event => { setFiles(Array.from(event.target.files || [])); setError(null) }} /></label>
            <button className="cv-send" type="submit" disabled={busy || !goal.trim()} aria-label="开始制作">
              {busy ? <span className="cv-spinner" style={{ borderTopColor: 'var(--cv-on-accent)' }} /> : <Icon name="arrow" width={20} height={20} />}
            </button>
          </div>
        </div>
      </form>
      {busy && <p className="cv-hint">{stage}</p>}
      {!busy && <p className="cv-hint">支持 ZIP、文档与样例，项目材料最大 1 GB。只有缺少必要信息时才会问你。</p>}
      {error && <div className="cv-error" role="alert"><strong>没有开始成功</strong><span>{error}</span>{attempt.current.project && <Link className="cv-verify-link" to={`/projects/${encodeURIComponent(String(attempt.current.project.id))}`}>工作区已保留，可打开查看</Link>}</div>}
      <p className="cv-example">例如：<b>做一个能预约、改期和导出记录的管理工具</b></p>
    </div>
  )
}
