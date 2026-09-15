import { useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import Icon from './Icon'
import SkillUploadFeedback from './SkillUploadFeedback'
import SkillIngestion from './SkillIngestion'
import { errorText, type PageProps } from './ui'

type Routed = { channel: 'attachment' | 'adaptation'; message: string; ingestion?: { run_id: string } }

export default function AddAgentAbility({ agentId, onChanged, ...props }: PageProps & { agentId: string; onChanged: () => void }) {
  const [kind, setKind] = useState('prompt')
  const [text, setText] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [runId, setRunId] = useState('')
  const [epoch, setEpoch] = useState(0)
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
        setMessage('已读取目录，将走适配与人签；无需关联项目。'); setRunId(result.run_id)
      } else {
        const conversation = await request<{ id: string }>(`/api/v4/agents/${encodeURIComponent(agentId)}/conversations`, { ...options, body: { mode: 'maintain', project_id: null } })
        await request(`/api/v4/conversations/${conversation.id}/messages`, { ...options, body: { content: text } })
        setText(''); setMessage('Prompt 已作为维护会话素材保存，整理后请在维护对话应用草稿。')
      }
      setEpoch(v => v + 1)
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  return <section aria-label="为职能体添加能力">
    <details className="wb-card" open>
      <summary><Icon name="triangle" className="wb-disclosure-icon" />为职能体添加能力</summary>
      <p>装备岗位无需项目。系统会识别输入：Prompt 进入维护会话；单 skill 作为附件；多 skill 或大包进入适配与人签。</p>
      <div className="wb-import-tabs" role="group" aria-label="能力素材类型">{[['prompt', '粘贴 Prompt'], ['file', '选择文件'], ['directory', '读取目录']].map(([value, label]) => <button type="button" key={value} aria-pressed={kind === value} className="wb-button wb-button-secondary" disabled={busy} onClick={() => setKind(value)}>{label}</button>)}</div>
      {kind === 'prompt' && <><label>Prompt 素材<textarea value={text} onChange={e => setText(e.target.value)} maxLength={50000} disabled={busy} /></label><button className="wb-button wb-button-primary" disabled={busy || !text.trim()} onClick={() => void submit()}>交给维护会话整理</button></>}
      {kind === 'file' && <label>能力 ZIP 文件<input type="file" accept=".zip" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) void submit(file); e.target.value = '' }} /></label>}
      {kind === 'directory' && <><p>读取管理员预先挂载的资料目录；仅接受挂载根目录下的相对路径。</p><label>资料相对目录<input value={path} onChange={e => setPath(e.target.value)} disabled={busy} placeholder="external-skills/example" /></label><button className="wb-button wb-button-primary" disabled={busy || !path.trim()} onClick={() => void submit()}>读取并适配</button></>}
      {busy && <p role="status">正在识别并保存素材…</p>}
      {error && <SkillUploadFeedback message={error} />}
      {message && <p role="status">{message} {runId ? <Link to={`/runs/${runId}`}>查看适配运行</Link> : <Link to={`/agents/${agentId}?mode=maintain`}>打开维护对话</Link>}</p>}
    </details>
    <SkillIngestion key={`${agentId}:${epoch}`} agentId={agentId} onSigned={onChanged} {...props} />
  </section>
}
