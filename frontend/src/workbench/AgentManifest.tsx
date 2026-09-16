import Icon from './Icon'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import { ErrorNotice, errorText, type PageProps } from './ui'
import './agent-manifest.css'
export type SkillRef = { id: string; version: number }
import { skillLabel, type Skill } from './skill-label'
export { skillLabel } from './skill-label'
export type Manifest = { revision: number; identity: string; skills: SkillRef[]; assertions: string[]; compiler?: string; created_at?: string; resolved_skills?: Skill[]; history?: Manifest[] }
export default function AgentManifest({agentId,...props}:PageProps & {agentId:string}) {
  const [value,setValue]=useState<Manifest|null>(null),[skills,setSkills]=useState<Skill[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  const [identity,setIdentity]=useState(''),[assertions,setAssertions]=useState(''),[refs,setRefs]=useState<SkillRef[]>([])
  const base=`/api/v4/agents/${encodeURIComponent(agentId)}/manifest`
  const admin=props.user?.role==='admin'
  const load=useCallback(async(signal?:AbortSignal)=>{
    const [m,library]=await Promise.all([request<Manifest>(base,{signal,onUnauthorized:props.onUnauthorized}),request<{modules:Skill[]}>(`/api/v4/modules?agent_id=${encodeURIComponent(agentId)}`,{signal,onUnauthorized:props.onUnauthorized})])
    if(signal?.aborted)return
    setValue(m);setSkills(library.modules);setIdentity(m.identity);setAssertions(m.assertions.join('\n'));setRefs(m.skills)
  },[base,props.onUnauthorized])
  useEffect(()=>{const c=new AbortController();void load(c.signal).catch(e=>{if(!c.signal.aborted)setError(errorText(e))});return()=>c.abort()},[load])
  const mutate=async(path:string,body:unknown,method:'PUT'|'POST')=>{setBusy(true);setError('');try{await request(path,{method,body,csrfToken:props.csrfToken,onUnauthorized:props.onUnauthorized});await load()}catch(e){setError(errorText(e))}finally{setBusy(false)}}
  return <section className="wb-card agent-manifest" aria-label="职能体清单">
    <h2>岗位清单{value && <small> · revision {value.revision}</small>}</h2>
    <p>skill 是工具，职能体是岗位，运行是任务。身份段只由人编辑。</p>
    {error && <ErrorNotice message={error}/>}
    {value && <><form onSubmit={e=>{e.preventDefault();void mutate(base,{revision:value.revision,identity,skills:refs,assertions:assertions.split('\n').filter(s=>s.trim())},'PUT')}}>
      <label>身份段<textarea maxLength={1200} rows={5} value={identity} disabled={!admin||busy} onChange={e=>setIdentity(e.target.value)} placeholder="我是谁、管什么、不管什么、交付口吻"/></label>
      <h3>已装备能力 · {refs.length} 项</h3>{!refs.length && <p role="status">岗位清单尚未引用 skill。目前只使用身份说明；已上传资料需完成整理或适配、人签后加入清单，才会用于新任务。</p>}<h3>引用的 skill</h3><ul>{refs.map(ref=>{const skill=value.resolved_skills?.find(s=>s.id===ref.id&&s.version===ref.version)||skills.find(s=>s.id===ref.id);return <li key={ref.id}><Link to={`/ability-center?tab=modules&selected=${encodeURIComponent(ref.id)}&agent_id=${encodeURIComponent(agentId)}`}>{skill ? skillLabel({...skills.find(s=>s.id===ref.id),...skill}) : ref.id}</Link> · v{ref.version} · 来源：{skill?.source?.type||skill?.actor||'能力模块'} {admin && <><button type="button" disabled={busy} onClick={()=>setRefs(refs.filter(s=>s.id!==ref.id))}>移除 {skill ? skillLabel({...skills.find(s=>s.id===ref.id),...skill}) : ref.id}</button>{skills.some(s=>s.id===ref.id&&s.version>ref.version)&&<button type="button" disabled={busy} onClick={()=>setRefs(refs.map(s=>s.id===ref.id?{id:s.id,version:skills.find(m=>m.id===s.id)!.version}:s))}>采用最新版本</button>}</>}</li>})}</ul>
      {admin && <label>加入 skill<select value="" disabled={busy} onChange={e=>{const m=skills.find(s=>s.id===e.target.value);if(m)setRefs([...refs,{id:m.id,version:m.version}])}}><option value="">选择能力模块及版本</option>{skills.filter(s=>!refs.some(r=>r.id===s.id)).map(s=><option key={s.id} value={s.id}>{skillLabel(s)} · v{s.version}</option>)}</select></label>}
      <label>验收断言（每行一项）<textarea rows={4} value={assertions} disabled={!admin||busy} onChange={e=>setAssertions(e.target.value)}/></label>
      {value.compiler==='legacy-exact-v1' && <p>当前使用逐字保真的遗留编译；保存清单后启用身份段与能力单元编排。</p>}
      {admin&&<button className="wb-button wb-button-primary" disabled={busy}>保存清单</button>}
    </form><a className="wb-text-link" href={`/api/v4/agents/${encodeURIComponent(agentId)}/pack`}>导出职能包 v2 ZIP</a>
    <details><summary><Icon name="triangle" className="wb-disclosure-icon" />清单历史</summary>{value.history?.map(m=><article key={m.revision}><h3>revision {m.revision}</h3><p>{m.identity}</p><p>{m.skills.length} 个 skill · {m.assertions.length} 项断言</p><pre>{JSON.stringify({skills:m.skills,assertions:m.assertions},null,2)}</pre>{admin&&m.revision!==value.revision&&<button disabled={busy} onClick={()=>void mutate(base+'/restore',{revision:value.revision,target_revision:m.revision},'POST')}>恢复 revision {m.revision}</button>}</article>)}</details></>}
  </section>
}
