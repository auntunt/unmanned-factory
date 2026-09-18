import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import { ProvenanceContext, capabilityHref, type Provenance } from './capability-links'
import type { Capability } from './v3-types'
import type { PageProps } from './ui'
import { PageHeader } from './ui'
import { tabKeys } from './presentation'
import ModulesPage from './ModulesPage'
import CapabilitiesPage from './CapabilitiesPage'
import PacksPage from './PacksPage'

export default function CapabilityCenter(props: PageProps) {
  const [params] = useSearchParams(); const requested = params.get('tab'); const tab = requested === 'capabilities' || requested === 'packs' ? requested : 'modules'
  const [provenance, setProvenance] = useState<Provenance>({ byRun: new Map(), counts: new Map(), loaded: false, error: false })
  useEffect(() => { const controller = new AbortController(); void Promise.all([
    request<{ capabilities: Capability[] }>('/api/v3/capabilities', { onUnauthorized: props.onUnauthorized, signal: controller.signal }),
    request<{ runs: Array<Run & { module_snapshot?: Array<{ id: string; name: string }> }> }>('/api/v2/runs', { onUnauthorized: props.onUnauthorized, signal: controller.signal }),
  ]).then(([capabilities, runs]) => {
    const byRun = new Map(runs.runs.filter(run => Array.isArray(run.module_snapshot)).map(run => [String(run.id), [...new Map(run.module_snapshot!.map(module => [module.id,module])).values()]]))
    const counts = new Map<string, number>()
    for (const capability of capabilities.capabilities) for (const id of new Set((byRun.get(capability.source_run_id || '') || []).map(module => module.id))) counts.set(id, (counts.get(id) || 0) + 1)
    if (!controller.signal.aborted) setProvenance({ byRun, counts, loaded: true, error: false })
  }).catch(() => { if (!controller.signal.aborted) setProvenance({ byRun: new Map(), counts: new Map(), loaded: true, error: true }) }); return () => controller.abort() }, [props.onUnauthorized])
  return <div className="wb-page wb-ability-center"><PageHeader title="能力中心" description="搭配工作方法，复用运行中沉淀的经验。" /><nav className="wb-ability-tabs" role="tablist" aria-label="能力分类" onKeyDown={tabKeys}>{(['modules','capabilities','packs'] as const).map(key => <Link role="tab" id={`ability-tab-${key}`} aria-controls="ability-panel" aria-selected={tab === key} tabIndex={tab === key ? 0 : -1} key={key} to={capabilityHref(key)}>{key === 'modules' ? '方法模块（Skill · 方法知识）' : key === 'capabilities' ? '沉淀能力' : '职能包（独立工具）'}</Link>)}</nav><ProvenanceContext.Provider value={provenance}><section role="tabpanel" id="ability-panel" aria-labelledby={`ability-tab-${tab}`}>{tab === 'modules' ? <ModulesPage {...props} embedded /> : tab === 'capabilities' ? <CapabilitiesPage {...props} embedded /> : <PacksPage {...props} embedded />}</section></ProvenanceContext.Provider></div>
}
