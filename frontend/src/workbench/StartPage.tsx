import { useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { ErrorNotice, PageHeader, errorText, type PageProps } from './ui'
import { validateProjectFiles, validateProjectZip, type ProjectRecord } from './ProjectsPage'

export default function StartPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const navigate = useNavigate()
  const [goal, setGoal] = useState(() => { try { return localStorage.getItem('webuddy:start:goal') || '' } catch { return '' } })
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stage, setStage] = useState('')
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
  if (user?.role === 'member') return <div className="wb-page"><PageHeader title="开始制作" description="打开已分配的作品，直接描述你想完成的事。" /><Link className="wb-button wb-button-primary" to="/projects">我的作品</Link></div>
  return <div className="wb-page wb-start-page"><PageHeader title="你想做出什么？" description="描述目标，附上已有材料。我们会准备工作区，完成实现、验证与修复，并在任务中提供成果。" /><section className="wb-card wb-requirement-card"><form className="wb-form" onSubmit={submit} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.requestSubmit() } }}>
    <label>描述你想要的产品<textarea required maxLength={50000} rows={7} disabled={busy || Boolean(attempt.current.project)} value={goal} onChange={event => { setGoal(event.target.value); try { localStorage.setItem('webuddy:start:goal', event.target.value) } catch { /* Storage is optional. */ } }} placeholder="例如：做一个客户订单管理应用，可以录入、搜索和导出订单。" /></label>
    <label>添加材料（可选）<input type="file" multiple disabled={busy || Boolean(attempt.current.project)} onChange={event => setFiles(Array.from(event.target.files || []))} /><small>支持项目 ZIP、样例、图片和文档，最多 100 个文件、约 1 GB。</small></label>
    {error && <ErrorNotice message={error} />}
    {attempt.current.project && error && <p>工作区已保留，重试会从提交任务继续。<Link to={`/projects/${encodeURIComponent(String(attempt.current.project.id))}`}>打开工作区</Link></p>}
    <div className="wb-form-actions"><span className="wb-runtime-note">只有缺少必要信息时才需要你补充。</span><button className="wb-button wb-button-primary" disabled={busy || !goal.trim()}>{busy ? stage : error && attempt.current.project ? '继续制作' : '开始制作'}</button></div>
  </form></section><p><Link className="wb-text-link" to="/projects">查看我的作品</Link></p></div>
}
