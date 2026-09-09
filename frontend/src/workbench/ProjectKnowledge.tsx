import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, errorText, type PageProps } from './ui'
import './project-knowledge.css'

export type Helper = { id: string; name: string; purpose?: string; active_version: number; version?: { version: number; instructions: string; acceptance: string[]; skill_ids: string[] } }
type Binding = { revision: number; agent_id: string | null; agent?: Helper; skills?: { id: string; filename: string }[] }
type Learning = { id: string; revision: number; title: string; content: string; source: string; run_id?: string; disposition?: { destination: string; agent_id?: string; agent_name?: string; applied?: boolean; source_revision: number; source?: { content: string } } }

export default function ProjectKnowledge({ projectId, csrfToken, onUnauthorized }: PageProps & { projectId: string | number }) {
  const [binding, setBinding] = useState<Binding | null>(null)
  const [helpers, setHelpers] = useState<Helper[]>([])
  const [learnings, setLearnings] = useState<Learning[]>([])
  const [selection, setSelection] = useState('')
  const [target, setTarget] = useState('')
  const [selected, setSelected] = useState<Learning | null>(null)
  const [destination, setDestination] = useState('standalone')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [adding, setAdding] = useState(false)
  const base = `/api/v4/projects/${encodeURIComponent(String(projectId))}`
  const load = useCallback(async (signal?: AbortSignal) => {
    const [helper, agents, entries] = await Promise.all([
      request<Binding>(`${base}/assistant`, { onUnauthorized, signal }),
      request<{ agents: Helper[] }>('/api/v4/agents', { onUnauthorized, signal }),
      request<{ learnings: Learning[] }>(`${base}/learnings`, { onUnauthorized, signal }),
    ])
    if (signal?.aborted) return
    setBinding(helper); setSelection(helper.agent_id || ''); setHelpers(agents.agents); setLearnings(entries.learnings)
  }, [base, onUnauthorized])
  useEffect(() => { const controller = new AbortController(); void load(controller.signal).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) }); return () => controller.abort() }, [load])
  const mutate = async (operation: () => Promise<void>) => { setBusy(true); setError(null); setNotice(''); try { await operation(); await load() } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) } }
  const saveBinding = () => mutate(async () => {
    if (!binding) return
    await request(`${base}/assistant`, { method: 'PUT', csrfToken, onUnauthorized, body: { agent_id: selection || null, expected_revision: binding.revision } })
    setNotice('已保存。后续新任务会使用所选职能体，进行中的任务保留原版本。')
  })
  const settle = () => mutate(async () => {
    if (!selected) return
    const draft = destination === 'agent' ? await request<{ revision: number }>(`/api/v4/agents/${encodeURIComponent(target)}/draft`, { onUnauthorized }) : null
    await request(`${base}/learnings/settle`, { method: 'POST', csrfToken, onUnauthorized, body: { source_id: selected.id, source_revision: selected.revision, destination, agent_id: destination === 'agent' ? target : null, draft_revision: draft?.revision || 0 } })
    setSelected(null); setNotice(destination === 'agent' ? '已合并到职能体草稿。查看变更并应用后，后续任务即可使用。' : '已单独保存，来源和内容保留在本项目。')
  })
  const add = () => mutate(async () => {
    await request(`/api/v2/projects/${encodeURIComponent(String(projectId))}/knowledge`, { method: 'POST', csrfToken, onUnauthorized, body: { kind: 'hypothesis', status: 'candidate', title: title.trim(), content: content.trim(), paths: [] } })
    setTitle(''); setContent(''); setAdding(false); setNotice('已记下这条收获，可以继续选择沉淀去向。')
  })
  const pending = learnings.filter((item) => !item.disposition)
  const saved = learnings.filter((item) => item.disposition)
  return <div className="pk-workspace">
    {error && <ErrorNotice message={error} />}{notice && <p role="status" className="wb-notice">{notice}</p>}
    <section className="pk-section"><div className="pk-heading"><div><span className="wb-eyebrow">项目的工作方法</span><h2>选择智能体帮助</h2><p>职能体把 Skill、工作步骤和验收经验带入这个项目。</p></div><Link to="/agents">管理职能体 →</Link></div>
      <div className="pk-picker"><label htmlFor="project-helper">负责本项目的职能体</label><select id="project-helper" value={selection} onChange={(event) => setSelection(event.target.value)} disabled={busy || !binding}><option value="">使用平台通用助手</option>{helpers.map((helper) => <option key={helper.id} value={helper.id}>{helper.name}</option>)}</select><button className="wb-button wb-button-primary" disabled={busy || !binding || selection === (binding.agent_id || '')} onClick={() => void saveBinding()}>保存选择</button></div>
      {binding?.agent ? <div className="pk-helper"><div><h3>{binding.agent.name} <small>v{binding.agent.version?.version}</small></h3><p>{binding.agent.purpose || '通过维护对话补充它擅长的工作。'}</p><Link to={`/agents/${binding.agent.id}?mode=maintain`}>维护工作方法与 Skill →</Link></div><div><strong>已采用的 Skill</strong><p>{binding.skills?.filter((skill) => binding.agent?.version?.skill_ids.includes(skill.id)).map((skill) => skill.filename).join('、') || '尚未应用 Skill，可在维护对话中添加。'}</p><details><summary>查看工作步骤与验收条件</summary><p className="pk-text">{binding.agent.version?.instructions || '尚未补充工作步骤'}</p><ul>{binding.agent.version?.acceptance.map((item) => <li key={item}>{item}</li>)}</ul></details></div></div> : <p>尚未关联专门的职能体，项目仍可使用通用助手开始任务。</p>}
    </section>
    <section className="pk-section"><div className="pk-heading"><div><span className="wb-eyebrow">来自项目知识与交付</span><h2>新的收获 <small>{pending.length} 条待整理</small></h2><p>先核对适用范围，再决定单独留存，还是合并成职能体的工作方法。</p></div><div className="pk-actions"><button className="wb-button wb-button-secondary" disabled={busy} onClick={() => void mutate(async () => {})}>刷新</button><button className="wb-button wb-button-secondary" onClick={() => setAdding(!adding)}>＋ 记一条收获</button></div></div>
      {adding && <form className="pk-editor" onSubmit={(event) => { event.preventDefault(); void add() }}><label>收获标题<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>具体做法与适用范围<textarea required rows={4} maxLength={8000} value={content} onChange={(event) => setContent(event.target.value)} /></label><button className="wb-button wb-button-primary" disabled={busy || !title.trim() || !content.trim()}>保存收获</button></form>}
      {!pending.length && <p className="pk-empty">还没有待整理的收获。交付产生的能力草稿和新增项目知识会集中出现在这里。</p>}
      {pending.map((item) => <article className="pk-learning" key={item.id}><div><small>{item.source} · 修订 {item.revision}</small><h3>{item.title}</h3><details><summary>查看收获内容</summary><p className="pk-text">{item.content}</p></details>{item.run_id && <Link to={`/runs/${item.run_id}`}>查看来源任务 →</Link>}</div><button className="wb-button wb-button-secondary" onClick={() => { setSelected(item); setDestination('standalone'); setTarget(binding?.agent_id || '') }}>选择沉淀去向</button></article>)}
      {selected && <section className="pk-editor" aria-label="沉淀去向"><h3>整理：{selected.title}</h3><label>沉淀去向<select value={destination} onChange={(event) => setDestination(event.target.value)}><option value="standalone">单独沉淀 · 保留为项目独立收获</option><option value="agent">合并到职能体 · 形成可应用的草稿</option></select></label>{destination === 'agent' && <label>目标职能体<select value={target} onChange={(event) => setTarget(event.target.value)}><option value="">请选择职能体</option>{helpers.map((helper) => <option key={helper.id} value={helper.id}>{helper.name}</option>)}</select></label>}<p>{destination === 'agent' ? '原始收获和来源任务会保留。合并后请查看职能体草稿，应用更新才会影响后续任务。' : '独立保留本次收获，不修改职能体的通用做法。'}</p><div className="pk-actions"><button className="wb-button wb-button-primary" disabled={busy || (destination === 'agent' && !target)} onClick={() => void settle()}>{busy ? '保存中…' : '确认沉淀'}</button><button className="wb-button wb-button-secondary" disabled={busy} onClick={() => setSelected(null)}>取消</button></div></section>}
    </section>
    <section className="pk-section"><div className="pk-heading"><div><h2>已沉淀 <small>{saved.length} 条</small></h2><p>每条收获都能追溯来源，也能看清最终归属。</p></div></div>{!saved.length && <p className="pk-empty">完成上方整理后，这里会显示沉淀记录。</p>}{saved.map((item) => <article className="pk-learning" key={item.id}><div><h3>{item.title}</h3><p>{item.disposition?.destination === 'standalone' ? '单独沉淀 · 本项目' : `合并到 ${item.disposition?.agent_name} · ${item.disposition?.applied ? '已应用' : '待应用草稿'}`}</p><small>{item.source} · 来源修订 {item.disposition?.source_revision}</small><details><summary>查看原始内容</summary><p className="pk-text">{item.disposition?.source?.content || item.content}</p></details></div>{item.disposition?.destination === 'standalone' && <a href={`${base}/learnings/export?source_id=${encodeURIComponent(item.id)}`}>导出 Skill</a>}{item.disposition?.agent_id && <Link to={`/agents/${item.disposition.agent_id}?mode=maintain`}>查看职能体 →</Link>}</article>)}</section>
  </div>
}
