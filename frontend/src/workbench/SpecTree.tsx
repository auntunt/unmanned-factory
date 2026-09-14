import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import Markdown from './Markdown'
import { request } from '../workspace/api'
import type { Project } from '../workspace/types'
import { ErrorNotice, errorText, type PageProps } from './ui'
import './spec-tree.css'

type Commit = { sha: string; subject: string; timestamp?: number }
type Entry = { entry: string; path: string; symbol: string | null }
export type SpecNode = { path: string; title: string; status: string; depth: number; code_count: number; desc: string; errors: string[]; raw_source: string; expanded: string; code: Entry[]; related: Entry[]; history: Commit[]; drift: { level: 'none'|'file'|'anchored'; unverified?: boolean; commits: Commit[]; reasons: string[] } }
export function SpecDriftBadge({ drift }: Pick<SpecNode,'drift'>) {
  const tone = drift.level === 'anchored' ? 'danger' : drift.level === 'file' || drift.unverified ? 'warning' : 'success'
  return <span className={`wb-status wb-status-${tone}`}>{drift.unverified ? '未验证' : {none:'无漂移',file:'文件级漂移',anchored:'锚定漂移'}[drift.level]}</span>
}
export function SpecEvidenceLink({ projectId, path, runId }: { projectId: string|number; path: string; runId?: string|number }) {
  return <Link to={`/projects/${encodeURIComponent(String(projectId))}?tab=spec&node=${encodeURIComponent(path)}${runId ? `&run=${encodeURIComponent(String(runId))}` : ''}`}>查看对应规格节点</Link>
}
function Commits({ items }: { items: Commit[] }) {
  return items.length ? <ul>{items.map(c => <li key={c.sha}><code title={c.sha}>{c.sha.slice(0,8)}</code> {c.subject}</li>)}</ul> : <p>暂无提交记录</p>
}
export function SpecNodeDetail({ node }: { node: SpecNode }) {
  return <article className="wb-card spec-detail"><h2>{node.title}</h2>{node.errors.length > 0 && <div role="alert">{node.errors.join('；')}</div>}
    <section className="spec-raw"><h3>人签意图 · raw source</h3><pre>{node.raw_source || '尚未人签，等待确认意图。'}</pre></section>
    <section><h3>展开规格 · expanded</h3><div className="spec-markdown"><Markdown>{node.expanded || '尚未填写展开规格。'}</Markdown></div></section>
    <section><h3>管辖文件</h3><ul>{node.code.map(e => <li key={e.entry}><code>{e.path}</code> {e.symbol && <span className="wb-status">#{e.symbol}</span>}</li>)}</ul>{!node.code.length && <p>尚未声明管辖文件</p>}</section>
    <section><h3>相关文件 · related</h3><ul>{node.related.map(e => <li key={e.entry}><code>{e.entry}</code></li>)}</ul>{!node.related.length && <p>没有相关文件</p>}</section>
    <section><h3>漂移状态</h3><SpecDriftBadge drift={node.drift}/>{node.drift.reasons.map(r => <p key={r}>{r}</p>)}<Commits items={node.drift.commits}/></section>
    <section><h3>版本历史</h3><Commits items={node.history}/></section>
  </article>
}
export default function SpecTree({ projectId, onUnauthorized }: { projectId: string; onUnauthorized: PageProps['onUnauthorized'] }) {
  const [params,setParams] = useSearchParams()
  const [data,setData] = useState<{nodes:SpecNode[];drift_count:number;error?:string}|null>(null)
  const [node,setNode] = useState<SpecNode|null>(null)
  const [error,setError] = useState('')
  const selected = params.get('node')
  const runId = params.get('run')
  const runQuery = runId ? `run_id=${encodeURIComponent(runId)}` : ''
  const base = `/api/v2/projects/${encodeURIComponent(projectId)}/spec-tree`
  useEffect(() => { const c = new AbortController(); setData(null); setError(''); void request<NonNullable<typeof data>>(`${base}${runQuery ? `?${runQuery}` : ''}`,{signal:c.signal,onUnauthorized}).then(d => { if(!c.signal.aborted)setData(d) }).catch(e => {if(!c.signal.aborted)setError(errorText(e))}); return () => c.abort() },[base,runQuery,onUnauthorized])
  useEffect(() => { const c = new AbortController(); setNode(null); setError(''); if(selected)void request<SpecNode>(`${base}/node?path=${encodeURIComponent(selected)}${runQuery ? `&${runQuery}` : ''}`,{signal:c.signal,onUnauthorized}).then(d=>{if(!c.signal.aborted)setNode(d)}).catch(e=>{if(!c.signal.aborted)setError(errorText(e))}); return ()=>c.abort() },[base,selected,runQuery,onUnauthorized])
  return <section aria-label="规格树"><h2>规格树</h2>{runId && <p>正在查看运行 {runId.slice(0,8)} 的提交快照。<Link to={`/projects/${projectId}?tab=spec`}>查看项目当前版本</Link></p>}{error && <ErrorNotice message={error}/>}<p>{data ? `${data.nodes.length} 节点 · ${data.drift_count} 处 drift` : '正在读取规格树…'}</p>{data?.error && <ErrorNotice message={data.error}/>}<div className="spec-layout"><ul className="spec-list" aria-label="规格节点">{data?.nodes.map(n=><li key={n.path} style={{paddingInlineStart:Math.min(n.depth,8)*16}}><button className="spec-row" aria-pressed={selected===n.path} onClick={()=>{const p=new URLSearchParams(params);p.set('node',n.path);setParams(p)}}><strong>{n.title}</strong><span className="wb-status">{n.status}</span><SpecDriftBadge drift={n.drift}/><small>{n.code_count} 个文件</small></button></li>)}</ul>{node ? <SpecNodeDetail node={node}/> : <p>{selected ? '正在读取节点…' : '选择节点查看人签意图、代码关联与版本历史。'}</p>}</div></section>
}
export function SpecSettings({ project, onSaved, csrfToken, onUnauthorized }: {project: Project & {revision?:number;spec_tree_enabled?:boolean};onSaved:(project: Project & {revision?:number})=>void} & Pick<PageProps,'csrfToken'|'onUnauthorized'>) {
  const navigate=useNavigate();const [busy,setBusy]=useState(false);const [error,setError]=useState('');const key=useRef(crypto.randomUUID())
  const base=`/api/v2/projects/${encodeURIComponent(String(project.id))}/spec-tree`
  const configure=async()=>{setBusy(true);setError('');try{onSaved(await request(`${base}/settings`,{method:'PUT',csrfToken,onUnauthorized,body:JSON.stringify({enabled:!project.spec_tree_enabled,revision:project.revision ?? 1})}))}catch(e){setError(errorText(e))}finally{setBusy(false)}}
  const generate=async()=>{setBusy(true);setError('');try{const run=await request<{id:string}>(`${base}/generate`,{method:'POST',csrfToken,onUnauthorized,body:JSON.stringify({idempotency_key:key.current})});navigate(`/runs/${run.id}`)}catch(e){setError(errorText(e));setBusy(false)}}
  return <section className="wb-card"><h2>规格树</h2><p>将项目意图与代码关联，用 Git 历史检查漂移。首次启用由平台归档纯规格骨架；不安装钩子或改写代理配置。</p><button className="wb-button" disabled={busy} aria-pressed={!!project.spec_tree_enabled} onClick={()=>void configure()}>{project.spec_tree_enabled?'关闭规格树':'启用规格树'}</button>{project.spec_tree_enabled && <button className="wb-button" disabled={busy} onClick={()=>void generate()}>生成规格树</button>}<p>生成初稿会创建普通运行，沿用项目预算与独立验收；新节点的人签意图留空。</p>{error && <ErrorNotice message={error}/>}</section>
}
