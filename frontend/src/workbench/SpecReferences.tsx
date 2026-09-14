import { useEffect, useMemo, useRef, useState, type TextareaHTMLAttributes } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import type { PageProps } from './ui'
import './spec-tree.css'

type Node = {path:string;title:string;status?:string;errors?:string[]}
type Props = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>,'value'|'onChange'> & {
  projectId:string;enabled:boolean;value:string;onChange:(value:string)=>void;onUnauthorized:PageProps['onUnauthorized']
}
export function SpecReferenceInput({projectId,enabled,value,onChange,onUnauthorized,...props}:Props) {
  const input=useRef<HTMLTextAreaElement>(null)
  const [nodes,setNodes]=useState<Node[]>([]);const [caret,setCaret]=useState(0);const [active,setActive]=useState(0);const [dismissed,setDismissed]=useState(false);const [failed,setFailed]=useState(false)
  useEffect(()=>{setNodes([]);setFailed(false);if(!enabled)return;const c=new AbortController();void request<{nodes:Node[]}>(`/api/v2/projects/${encodeURIComponent(projectId)}/spec-tree`,{signal:c.signal,onUnauthorized}).then(data=>{if(!c.signal.aborted)setNodes(data.nodes)}).catch(()=>{if(!c.signal.aborted)setFailed(true)});return()=>c.abort()},[projectId,enabled,onUnauthorized])
  const suggestions=useMemo(()=>{
    const counts=new Map<string,Set<string>>()
    for(const n of nodes)for(const name of [n.title,n.path.split('/').slice(-2)[0] ?? '']){const set=counts.get(name)??new Set<string>();set.add(n.path);counts.set(name,set)}
    return nodes.filter(n=>n.status!=='invalid'&&!n.errors?.length).flatMap(n=>{const names=[n.title,n.path.split('/').slice(-2)[0]??''];const name=names.find(s=>s&&counts.get(s)?.size===1&&!/[\[\]\r\n]/.test(s));return name?[{...n,name,search:names.join(' ').toLocaleLowerCase()}]:[]})
  },[nodes])
  const match=value.slice(0,caret).match(/\[\[([^\[\]\r\n]*)$/)
  const options=match?suggestions.filter(n=>n.search.includes(match[1].toLocaleLowerCase())):[]
  const open=enabled&&!props.disabled&&!dismissed&&!!match
  const listId=`${props.id ?? 'spec-reference'}-options`
  const selected=Math.min(active,Math.max(0,options.length-1))
  function choose(name:string){if(!match)return;const start=caret-match[0].length;const suffix=value.slice(caret).replace(/^\]\]/,'');const insert=`[[${name}]]`;const next=value.slice(0,start)+insert+suffix;if(next.length>(props.maxLength??50000))return;onChange(next);setCaret(start+insert.length);setDismissed(true);requestAnimationFrame(()=>{input.current?.focus();input.current?.setSelectionRange(start+insert.length,start+insert.length)})}
  return <><textarea {...props} ref={input} value={value} aria-autocomplete={enabled?'list':undefined} aria-haspopup={enabled?'listbox':undefined} aria-controls={open?listId:undefined} aria-activedescendant={open&&options.length?`${listId}-${selected}`:undefined}
    onChange={e=>{onChange(e.target.value);setCaret(e.target.selectionStart);setActive(0);setDismissed(false)}} onSelect={e=>setCaret(e.currentTarget.selectionStart)} onBlur={()=>setDismissed(true)}
    onKeyDown={e=>{if(open&&!e.nativeEvent.isComposing&&!e.ctrlKey&&!e.metaKey){if(e.key==='Escape'){e.preventDefault();setDismissed(true)}else if(options.length&&['ArrowDown','ArrowUp','Enter'].includes(e.key)){e.preventDefault();e.stopPropagation();if(e.key==='Enter')choose(options[selected].name);else setActive((selected+(e.key==='ArrowDown'?1:options.length-1))%options.length)}}}}/>
    {enabled&&<small>输入 [[ 引用规格节点；仅唯一匹配的节点生效。{failed?'节点建议暂不可用，仍可正常提交需求。':''}</small>}
    {open&&<ul id={listId} role="listbox" aria-label="规格节点建议" className="wb-spec-ref-options">{options.map((n,i)=><li id={`${listId}-${i}`} key={n.path} role="option" aria-selected={i===selected} onMouseDown={e=>e.preventDefault()} onClick={()=>choose(n.name)}>{n.name}<small>{n.path}</small></li>)}{!options.length&&<li role="presentation">没有可唯一匹配的节点</li>}</ul>}
  </>
}

type Ref = {name:string;path:string;commit:string;title?:string}
export function RunSpecReferences({source,projectId}:{source?:Record<string,unknown>;projectId:string|number}) {
  const refs=(Array.isArray(source?.spec_refs)?source.spec_refs:[]).filter((r):r is Ref=>!!r&&typeof r.path==='string'&&typeof r.commit==='string'&&typeof r.name==='string')
  const unmatched=(Array.isArray(source?.spec_ref_unmatched)?source.spec_ref_unmatched:[]).filter(r=>r&&typeof r.name==='string') as {name:string;reason:string}[]
  if(!refs.length&&!unmatched.length)return null
  return <section aria-label="需求引用的规格"><h3>引用规格</h3>{refs.length>0&&<ul>{refs.map(r=><li key={`${r.name}:${r.path}`}><Link to={`/projects/${encodeURIComponent(String(projectId))}?tab=spec&node=${encodeURIComponent(r.path)}`}>{r.title||r.name}</Link> <code title={r.commit}>{r.commit.slice(0,8)}</code></li>)}</ul>}{unmatched.length>0&&<div className="wb-notice"><strong>未匹配规格节点</strong><ul>{unmatched.map(r=><li key={r.name}>[[{r.name}]]：{r.reason==='ambiguous'?'存在多个同名节点':r.reason==='unavailable'?'规格树暂不可读':r.reason==='invalid'?'节点格式无效':'没有找到唯一节点'}；保留原始需求文本。</li>)}</ul></div>}</section>
}
