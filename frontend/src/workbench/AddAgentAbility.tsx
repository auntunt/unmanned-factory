import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import Icon from './Icon'
import AgentAssets from './AgentAssets'
import SkillUploadFeedback from './SkillUploadFeedback'
import SkillIngestion from './SkillIngestion'
import { errorText, type PageProps } from './ui'

type Routed = { channel: 'attachment' | 'adaptation'; message: string; ingestion?: { run_id: string } }

export default function AddAgentAbility({ agentId, onChanged, ...props }: PageProps & { agentId: string; onChanged: () => void }) {
  const [kind, setKind] = useState('file')
  const [text, setText] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [runId, setRunId] = useState('')
  const [epoch, setEpoch] = useState(0)
  const [preflight, setPreflight] = useState<{ ready: boolean; message: string } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    void request<{ ready: boolean; message: string }>(`/api/v4/agents/${encodeURIComponent(agentId)}/abilities/preflight`, { signal: controller.signal, onUnauthorized: props.onUnauthorized })
      .then(result => { if (!controller.signal.aborted) setPreflight(result) })
      .catch(e => { if (!controller.signal.aborted) setError(errorText(e)) })
    return () => controller.abort()
  }, [agentId, props.onUnauthorized])
  const submit = async (file?: File) => {
    if (busy) return
    setBusy(true); setError(''); setMessage(''); setRunId('')
    const options = { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized }
    try {
      if (file) {
        const body = new FormData(); body.append('file', file)
        const result = await request<Routed>(`/api/v4/agents/${encodeURIComponent(agentId)}/abilities`, { ...options, body })
        setMessage(result.message); setRunId(result.ingestion?.run_id || '')
      } else if (kind === 'directory') {
        const result = await request<{ run_id: string }>('/api/v4/skill-ingestions/directory', { ...options, body: { agent_id: agentId, path } })
        setMessage('已读取目录并启动自动准备，完成后在此核对启用。'); setRunId(result.run_id)
      } else {
        const conversation = await request<{ id: string }>(`/api/v4/agents/${encodeURIComponent(agentId)}/conversations`, { ...options, body: { mode: 'maintain', project_id: null } })
        await request(`/api/v4/conversations/${conversation.id}/messages`, { ...options, body: { content: text } })
        setText(''); setMessage('Prompt 已作为维护会话素材保存，整理后请在维护对话应用草稿。')
      }
      setEpoch(v => v + 1)
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  return <section aria-label="为职能体加载方法或安装工具">
    <details className="wb-card wb-form" open>
      <summary><Icon name="triangle" className="wb-disclosure-icon" />为职能体加载方法或安装工具</summary>
      <p>上传能力包即可自动准备和验收，无需项目或另开维护对话。完成后在此核对启用。</p>
      {preflight?.ready === false && <p role="alert">{preflight.message}</p>}
      <details><summary>其他资料来源</summary><div className="wb-import-tabs" role="group" aria-label="能力素材类型">{[['prompt', '粘贴 Prompt'], ['file', '选择文件'], ['directory', '读取目录']].map(([value, label]) => <button type="button" key={value} aria-pressed={kind === value} className="wb-button wb-button-secondary" disabled={busy} onClick={() => setKind(value)}>{label}</button>)}</div></details>
      {kind === 'prompt' && <><label>Prompt 素材<textarea value={text} onChange={e => setText(e.target.value)} maxLength={50000} disabled={busy} /></label><button className="wb-button wb-button-primary" disabled={busy || !text.trim()} onClick={() => void submit()}>交给维护会话整理</button></>}
      {kind === 'file' && <label>能力 ZIP 文件<input type="file" accept=".zip" disabled={busy || preflight?.ready === false} onChange={e => { const file = e.target.files?.[0]; if (file) void submit(file); e.target.value = '' }} /></label>}
      {kind === 'directory' && <><p>读取管理员预先挂载的资料目录；仅接受挂载根目录下的相对路径。</p><label>资料相对目录<input value={path} onChange={e => setPath(e.target.value)} disabled={busy} placeholder="external-skills/example" /></label><button className="wb-button wb-button-primary" disabled={busy || !path.trim() || preflight?.ready === false} onClick={() => void submit()}>读取并适配</button></>}
      {busy && <p role="status">正在识别并保存素材…</p>}
      {error && <SkillUploadFeedback message={error} />}
      {message && <p role="status">{message} {runId ? <Link to={`/runs/${runId}`}>查看适配运行</Link> : <Link to={`/agents/${agentId}?mode=maintain`}>打开维护对话</Link>}</p>}
    </details>
    <AgentAssets agentId={agentId} refresh={epoch} {...props} />
    <SkillIngestion key={`${agentId}:${epoch}`} agentId={agentId} onSigned={onChanged} {...props} />
  </section>
}
