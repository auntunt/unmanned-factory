import { createContext, useContext } from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'
import type { Capability } from './v3-types'
export interface Provenance { byRun: Map<string, Array<{ id: string; name: string }>>; counts: Map<string, number>; loaded: boolean; error: boolean }
export const ProvenanceContext = createContext<Provenance>({ byRun: new Map(), counts: new Map(), loaded: false, error: false })
export const useProvenance = () => useContext(ProvenanceContext)
export function capabilityHref(tab: 'modules' | 'capabilities', selected?: string) {
  return `/ability-center?${new URLSearchParams({ tab, ...(selected ? { selected } : {}) })}`
}
export function LegacyCapabilityRedirect({ tab }: { tab: 'modules' | 'capabilities' }) {
  const location = useLocation(); const params = new URLSearchParams(location.search); params.set('tab', tab)
  return <Navigate replace to={`/ability-center?${params}${location.hash}`} />
}
export function CapabilitySources({ capability }: { capability: Capability }) {
  const { byRun, error } = useProvenance()
  const modules = byRun.get(capability.source_run_id || '')
  return <div className="wb-source-links">{capability.source_run_id ? <><Link to={`/runs/${encodeURIComponent(capability.source_run_id)}`}>来源运行 {capability.source_run_id.slice(0,8)}</Link>{modules?.map(module => <Link key={module.id} to={capabilityHref('modules',module.id)}>来源模块：{module.name}</Link>)}{(!modules || error) && <span>来源模块未记录或暂不可见</span>}</> : <span>未记录来源运行</span>}</div>
}
