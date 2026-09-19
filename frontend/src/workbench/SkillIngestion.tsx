import Icon from './Icon'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, type PageProps } from './ui'

type Assertion = { text: string; kind: string; check: string; basis: string; basis_path?: string }
type Mapping = {
  identity: string
  authorization_required?: string[]
  steps: { title: string; skill_path: string; role: string; assertions: Assertion[] }[]
  decisions: { path: string; primitive: string; target: string; basis: string }[]
  dependencies: { path: string; reason: string }[]
  injection_risks: { path: string; reason: string; text?: string }[]
  skills: { path: string; name: string; requires_authorization: boolean; sha256: string; body: string }[]
}
type Draft = { runtime?: {status:string; error?:string; revision:number; resume_count?:number}; progress?: {mapped:number; verified:number; total?:number}; available_slots?: number; target_agent_id?: string; target_identity?: string; id: string; run_id: string; revision: number; status: string; source_sha256: string; mapping?: Mapping; agent_id?: string }

export function IngestionReview({ draft, onChanged, ...props }: PageProps & { draft: Draft; onChanged: () => void }) {
  const [mapping, setMapping] = useState(draft.mapping)
  const [identity, setIdentity] = useState(draft.target_identity ?? draft.mapping?.identity ?? '')
  const [flags, setFlags] = useState<Record<string, boolean>>(Object.fromEntries((draft.mapping?.skills || []).map(s => [s.path, s.requires_authorization])))
  const largeLibrary = (draft.mapping?.skills.length || 0) > (draft.available_slots ?? 24)
  const [selected, setSelected] = useState<string[]>([])
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [advanced, setAdvanced] = useState('')
  const editable = draft.status === 'review'
  const paused = draft.status !== 'review' && draft.status !== 'signed' && ['needs_human','failed','cancelled'].includes(draft.runtime?.status || '')
  const resume = async () => {
    if (!draft.runtime || busy) return
    setBusy(true); setError('')
    try { await request(`/api/v2/runs/${draft.run_id}/continue`, {method:'POST', csrfToken:props.csrfToken, onUnauthorized:props.onUnauthorized, body:{revision:draft.runtime.revision,resume_count:draft.runtime.resume_count || 0,answer:''}}); onChanged() }
    catch (e) { setError(errorText(e)) } finally {setBusy(false)}
  }
  const act = async (sign: boolean) => {
    if (!mapping) return
    setBusy(true); setError('')
    try {
      const body = { identity: mapping.identity, steps: mapping.steps, decisions: mapping.decisions,
        dependencies: mapping.dependencies, injection_risks: mapping.injection_risks,
        authorization_required: mapping.authorization_required || [] }
      await request(`/api/v4/skill-ingestions/${draft.id}${sign ? '/sign' : ''}`, {
        method: sign ? 'POST' : 'PUT', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized,
        body: sign ? { revision: draft.revision, identity, authorization: flags, ...(largeLibrary ? { selected_paths: selected } : {}) } : { revision: draft.revision, mapping: body },
      })
      onChanged()
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  return <section className="wb-card wb-form" aria-label="职能包适配评审">
    <h3>职能包适配 · {draft.status === 'review' ? '等待人签' : draft.status === 'signed' ? '已签署' : paused ? '适配已暂停，尚未启用' : draft.status === 'verifying' ? '独立验收中，尚未启用' : '适配中，尚未启用'}</h3>
    <Link to={`/runs/${draft.run_id}`}>查看适配运行与独立验收</Link>
    <p>{draft.status === 'signed' ? '已签署的能力已存入本职能体；只有岗位清单引用的能力会用于新任务。' : draft.status === 'review' ? '草稿已通过独立验收。请在此核对并签署，签署前不会用于任务。' : '资料包已保存，尚未成为可用能力。不必重复上传；适配和独立验收通过后在此人签。'}</p>
    {draft.progress && <p>已适配 {draft.progress.mapped} / {draft.progress.total ?? '待统计'} 个资料分片 · 已验收 {draft.progress.verified} 个</p>}
    {paused && <div role="alert"><p>{draft.runtime?.error || '任务已暂停，请查看运行记录。'}</p>{draft.runtime?.status === 'needs_human' && <button type="button" className="wb-button wb-button-primary" disabled={busy} onClick={() => void resume()}>{busy ? '正在恢复…' : '从已保存进度继续适配'}</button>}</div>}
    <details><summary>来源记录</summary><p>来源 SHA256：<code>{draft.source_sha256}</code></p></details>
    {draft.agent_id && <Link to={`/agents/${draft.agent_id}`}>打开正式职能体</Link>}
    {mapping && <>
      <details><summary>查看身份、映射与验收依据</summary>
      <label>人签身份段<textarea disabled={!editable || Boolean(draft.target_agent_id)} maxLength={1200} value={identity} onChange={e => setIdentity(e.target.value)} /></label>
      <h4>结构映射与步骤断言</h4>
      <ol>{mapping.steps.map((step, index) => <li key={`${step.skill_path}-${index}`}>
        <strong>{step.role === 'router' ? '路由根' : '叶子能力'}：{step.title}</strong><p>{step.skill_path}</p>
        {step.assertions.map((a, ai) => <div key={ai}>
          <p>{a.text}</p><blockquote>{a.basis}</blockquote>{a.basis_path && <small>依据来源：{a.basis_path}</small>}
          <label>核对类型<select value={a.kind} disabled={!editable} onChange={e => setMapping({ ...mapping, steps: mapping.steps.map((s, i) => i === index ? { ...s, assertions: s.assertions.map((v, j) => j === ai ? { ...v, kind: e.target.value } : v) } : s) })}><option value="mechanical">机械核对</option><option value="advisory">提示型</option></select></label>
          <label>核对方法<textarea value={a.check} disabled={!editable} onChange={e => setMapping({ ...mapping, steps: mapping.steps.map((s, i) => i === index ? { ...s, assertions: s.assertions.map((v, j) => j === ai ? { ...v, check: e.target.value } : v) } : s) })} /></label>
        </div>)}
      </li>)}</ol>
      <h4>宿主原语映射决议</h4>
      {mapping.decisions.map((d, i) => <div key={i}><strong>{d.primitive} → {d.target === 'unsupported' ? '该能力在本平台不可用' : d.target}</strong><p>{d.path}</p><label>映射依据<textarea disabled={!editable} value={d.basis} onChange={e => setMapping({ ...mapping, decisions: mapping.decisions.map((v, j) => j === i ? { ...v, basis: e.target.value } : v) })} /></label></div>)}
      </details>
      <h4>运行环境</h4><p>适配只准备方法与引用资料，不代表工具已安装或真实转换样例已通过。实际使用前仍需检查以下依赖。</p>{mapping.dependencies.length ? mapping.dependencies.map((d, i) => <p key={i}>{d.path}：{d.reason}</p>) : <p>未识别到额外依赖；尚未验证实际工具环境。</p>}
      <h4>注入风险条目</h4>{mapping.injection_risks.map((r, i) => <blockquote key={i}>{r.path}：{r.reason} {r.text}</blockquote>)}
      {largeLibrary && <p>这是能力库。人签保存全部能力；本次最多选择 {draft.available_slots ?? 24} 项加入岗位清单，其余保留为随附能力，后续可在清单中选择。</p>}
      <h4>安全映射与来源</h4>{mapping.skills.map(s => <div key={s.path}>
        {largeLibrary && <label><input type="checkbox" disabled={!editable || (!selected.includes(s.path) && selected.length >= (draft.available_slots ?? 24))} checked={selected.includes(s.path)} onChange={e => setSelected(e.target.checked ? [...selected, s.path] : selected.filter(p => p !== s.path))} />将 {s.name} 加入岗位清单</label>}
        <label><input type="checkbox" disabled={!editable} checked={flags[s.path]} onChange={e => setFlags({ ...flags, [s.path]: e.target.checked })} />{s.name}：执行前需要逐目标授权</label>
        <p>{s.path} · <code>{s.sha256}</code></p><details><summary><Icon name="triangle" className="wb-disclosure-icon" />原始正文（不可信资料）</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{s.body}</pre></details>
      </div>)}
      {editable && <details><summary><Icon name="triangle" className="wb-disclosure-icon" />调整完整结构、映射和依赖</summary>
        <p>修改完整映射数据；来源正文和哈希由平台保留，不允许替换。保存后重新核对再签署。</p>
        <button className="wb-button wb-button-secondary" onClick={() => setAdvanced(JSON.stringify({ identity: mapping.identity, steps: mapping.steps, decisions: mapping.decisions, dependencies: mapping.dependencies, injection_risks: mapping.injection_risks, authorization_required: mapping.authorization_required || [] }, null, 2))}>载入映射草稿</button>
        <label>映射 JSON<textarea rows={12} value={advanced} onChange={e => setAdvanced(e.target.value)} maxLength={200000} /></label>
        <button className="wb-button wb-button-secondary" disabled={busy || !advanced} onClick={async () => {
          setBusy(true); setError('')
          try { await request(`/api/v4/skill-ingestions/${draft.id}`, { method: 'PUT', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { revision: draft.revision, mapping: JSON.parse(advanced) } }); onChanged() }
          catch (e) { setError(errorText(e)) } finally { setBusy(false) }
        }}>校验并保存完整映射</button>
      </details>}
      {editable && <><p>修改映射后请先保存，刷新核对，再签署。签署不会执行外部脚本或自动安装工具。</p>
        <button className="wb-button wb-button-secondary" disabled={busy} onClick={() => void act(false)}>保存评审修改</button>
        <label><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已核对身份、全部授权标记、映射及不可用能力，同意启用</label>
        <button className="wb-button wb-button-primary" disabled={busy || !confirmed || (largeLibrary && !selected.length) || JSON.stringify(mapping) !== JSON.stringify(draft.mapping)} onClick={() => void act(true)}>人签并启用职能包</button></>}
    </>}
    {error && <p role="alert">{error}</p>}
  </section>
}

export default function SkillIngestion(props: PageProps & { agentId?: string; reviewOnly?: boolean; onSigned?: () => void }) {
  const [params] = useSearchParams()
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([])
  const [pid, setPid] = useState(params.get('project') || ''); const [path, setPath] = useState('')
  const [items, setItems] = useState<Draft[]>([]); const [epoch, setEpoch] = useState(0)
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const [uploaded, setUploaded] = useState<Draft | null>(null)
  useEffect(() => { if (props.agentId || props.reviewOnly) return; let active = true; void request<{ projects: { id: string; name: string }[] }>('/api/v2/projects', { onUnauthorized: props.onUnauthorized }).then(r => { if (active) setProjects(r.projects) }).catch(e => { if (active) setError(errorText(e)) }); return () => { active = false } }, [props.onUnauthorized, props.agentId, props.reviewOnly])
  useEffect(() => {
    if (!pid && !props.agentId) { setItems([]); return }
    let active = true
    const load = () => request<{ items: Draft[] }>(`/api/v4/skill-ingestions?${props.agentId ? `agent_id=${encodeURIComponent(props.agentId)}` : `project_id=${encodeURIComponent(pid)}`}`, { onUnauthorized: props.onUnauthorized }).then(r => { if (active) setItems(r.items) }).catch(e => { if (active) setError(errorText(e)) })
    void load(); const timer = window.setInterval(() => void load(), 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [pid, epoch, props.onUnauthorized, props.agentId])
  const submit = async (file?: File) => {
    if (busy || !pid) return
    setBusy(true); setError(''); setUploaded(null)
    try {
      if (file) {
        const body = new FormData(); body.append('file', file); body.append('project_id', pid)
        setUploaded(await request<Draft>('/api/v4/skill-ingestions', { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body }))
      } else setUploaded(await request<Draft>('/api/v4/skill-ingestions/directory', { method: 'POST', csrfToken: props.csrfToken, onUnauthorized: props.onUnauthorized, body: { project_id: pid, path } }))
      setEpoch(v => v + 1)
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  if (props.agentId || props.reviewOnly) return <div aria-label="能力适配与人签草稿">
    {error && <p role="alert">{error}</p>}
    {items.map(draft => <IngestionReview key={`${draft.id}:${draft.revision}`} draft={draft} {...props} onChanged={() => { setEpoch(v => v + 1); props.onSigned?.() }} />)}
  </div>
  return <section className="wb-card" aria-label="导入外部 skill 包"><h2>导入外部 skill 包</h2>
    <p>第三方下载或自己整理的 skill ZIP 都从这里导入。先选择所属项目，再上传文件。</p>
    <p>只读取、分类和映射。独立验收后进入评审，签署前不会启用。执行与验收须配置支持零工具模式的 Claude。</p>
    <label>所属项目<select disabled={busy} value={pid} onChange={e => setPid(e.target.value)}><option value="">请选择项目</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <label>上传外部 ZIP<input type="file" accept=".zip" disabled={!pid || busy} onChange={e => { const file = e.target.files?.[0]; if (file) void submit(file); e.target.value = '' }} /></label>
    <label>已挂载目录（相对项目工作区）<input value={path} onChange={e => setPath(e.target.value)} placeholder="external-skills/example" /></label>
    <button className="wb-button wb-button-secondary" disabled={!pid || !path || busy} onClick={() => void submit()}>读取目录并开始适配</button>
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">正在上传并创建适配运行…</p>}
    {uploaded && <p role="status">上传已接收，尚未启用。<Link to={`/runs/${uploaded.run_id}`}>查看适配运行</Link></p>}
    {items.map(draft => <IngestionReview key={`${draft.id}:${draft.revision}`} draft={draft} {...props} onChanged={() => setEpoch(v => v + 1)} />)}
  </section>
}
