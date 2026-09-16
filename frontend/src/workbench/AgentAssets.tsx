import { useEffect, useState } from 'react'
import { request } from '../workspace/api'
import { errorText, type PageProps } from './ui'

type Asset = { id:string; filename?:string; created_at?:string; files?: {path:string}[] }
export default function AgentAssets({agentId, refresh, ...props}:PageProps & {agentId:string; refresh:number}) {
  const [assets,setAssets] = useState<Asset[]>([]), [error,setError]=useState('')
  useEffect(()=>{const c=new AbortController(); void request<{skills:Asset[]}>(`/api/v4/agents/${encodeURIComponent(agentId)}/skills`,{signal:c.signal,onUnauthorized:props.onUnauthorized}).then(r=>{if(!c.signal.aborted){setAssets(r.skills || []);setError('')}}).catch(e=>{if(!c.signal.aborted)setError(errorText(e))});return()=>c.abort()},[agentId,refresh,props.onUnauthorized])
  return <section className="wb-card" aria-label="已上传资料"><h3>已上传资料 · {assets.length} 份</h3><p>这里是已保存的附件，不代表已经启用。实际用于工作的 skill 以“岗位清单”为准；能力包的自动准备状态见下方。</p>{error && <p role="alert">{error}</p>}{assets.map(a=><details key={a.id}><summary>{a.filename || '资料包'} · {a.files?.length ?? 0} 个文件</summary><ul>{a.files?.map(f=><li key={f.path}>{f.path}</li>)}</ul></details>)}{!assets.length && !error && <p>尚无归档附件。正在适配的资料包会在签署后归档。</p>}</section>
}
