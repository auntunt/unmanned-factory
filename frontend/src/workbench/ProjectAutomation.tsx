import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, errorText } from './ui'
import type { Capability, CapabilityBinding, Policy } from './v3-types'
import './project-autonomy.css'

interface Props { projectId: string; csrfToken: string; onUnauthorized: () => void; onPolicySaved?: () => void }
const defaultPolicy: Policy = { revision: 0, mode: 'autonomous', max_risk: 'medium', max_attempts: 2, auto_escalate: true, resume_on_restart: true }

export default function ProjectAutomation({ projectId, csrfToken, onUnauthorized, onPolicySaved }: Props) {
  const [policy, setPolicy] = useState<Policy | null>(null); const [draft, setDraft] = useState<Policy>(defaultPolicy); const [bindings, setBindings] = useState<CapabilityBinding[]>([]); const [capabilities, setCapabilities] = useState<Capability[]>([]); const [selected, setSelected] = useState(''); const [error, setError] = useState<string | null>(null); const [notice, setNotice] = useState<string | null>(null); const [busy, setBusy] = useState(false); const loadController = useRef<AbortController | null>(null)
  const noticeRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => { if (notice) noticeRef.current?.scrollIntoView({ block: 'nearest' }) }, [notice])
  const [applyWaiting, setApplyWaiting] = useState(true)
  const [application, setApplication] = useState<{ continued_run_ids: string[]; blocked: Array<{ run_id: string; reason: string }> } | null>(null)
  const load = () => { loadController.current?.abort(); const controller = new AbortController(); loadController.current = controller; setError(null); const promise = Promise.all([request<Policy>(`/api/v3/projects/${encodeURIComponent(projectId)}/policy`, { onUnauthorized, signal: controller.signal }), request<{ bindings: CapabilityBinding[] }>(`/api/v3/projects/${encodeURIComponent(projectId)}/capabilities`, { onUnauthorized, signal: controller.signal }), request<{ capabilities: Capability[] }>('/api/v3/capabilities', { onUnauthorized, signal: controller.signal })]).then(([nextPolicy, nextBindings, nextCapabilities]) => { if (!controller.signal.aborted) { setPolicy(nextPolicy); setDraft(nextPolicy); setBindings(nextBindings.bindings); setCapabilities(nextCapabilities.capabilities) } }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) }); return { promise, controller } }
  useEffect(() => { const loaded = load(); return () => loaded.controller.abort() }, [projectId, onUnauthorized])
  const ready = useMemo(() => capabilities.filter((item) => item.status === 'ready' && bindings.every((binding) => binding.capability_id !== item.id || binding.revision !== item.revision)), [bindings, capabilities])
  const savePolicy = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null); setNotice(null); setApplication(null)
    try {
      const next = await request<Policy & { application?: { continued_run_ids: string[]; blocked: Array<{ run_id: string; reason: string }> } }>(`/api/v3/projects/${encodeURIComponent(projectId)}/policy`, {
        method: 'PUT', csrfToken, onUnauthorized,
        body: { ...draft, revision: policy?.revision ?? 0, apply_waiting: draft.mode === 'autonomous' && applyWaiting },
      })
      const { application: applied, ...saved } = next
      setPolicy(saved); setDraft(saved); setApplication(applied ?? null); onPolicySaved?.()
      setNotice(saved.mode === 'autonomous'
        ? `Auto 已保存为策略版本 ${saved.revision}。后续需求在配置范围内自动规划、执行和验证。${applied ? `已有 ${applied.continued_run_ids.length} 条待执行计划自动接续。` : ''}`
        : `监督模式已保存为策略版本 ${saved.revision}，后续计划将等待人工批准。`)
    } catch (cause) {
      if (cause instanceof WorkspaceApiError && cause.status === 409) { await load().promise; setError(`策略版本已变化：${cause.detail} 请重新读取后再保存。`) }
      else setError(errorText(cause))
    } finally { setBusy(false) }
  }
  const bind = async () => { if (!selected) return; setBusy(true); setError(null); try { const binding = await request<CapabilityBinding>(`/api/v3/projects/${encodeURIComponent(projectId)}/capabilities`, { method: 'POST', csrfToken, onUnauthorized, body: { capability_id: selected, revision: capabilities.find((item) => item.id === selected)?.revision } }); setBindings((items) => [...items.filter((item) => item.capability_id !== binding.capability_id), binding]); setSelected(''); setNotice('能力版本已绑定到项目。') } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) } }
  const unbind = async (capabilityId: string) => { setBusy(true); setError(null); try { await request(`/api/v3/projects/${encodeURIComponent(projectId)}/capabilities/${encodeURIComponent(capabilityId)}`, { method: 'DELETE', csrfToken, onUnauthorized }); setBindings((items) => items.filter((item) => item.capability_id !== capabilityId)); setNotice('项目绑定已移除。') } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) } }
  if (!policy && !error) return <div className="wb-loading" role="status">正在读取自主策略和能力绑定…</div>
  if (!policy && error) return <div className="wb-automation-stack"><ErrorNotice message={error} /><button className="wb-button wb-button-secondary" onClick={() => void load().promise}>重新读取项目策略</button></div>
  const selectedCapability = capabilities.find((item) => item.id === selected)
  const selectedBinding = selectedCapability && bindings.find((item) => item.capability_id === selectedCapability.id)
  const isVersionUpdate = Boolean(selectedCapability && selectedBinding && selectedBinding.revision !== selectedCapability.revision)
  return <div className="wb-automation-stack">
    {error && <ErrorNotice message={error} />}
    {notice && <div ref={noticeRef} className="wb-notice" role="status">{notice}</div>}
    {application && (application.continued_run_ids.length > 0 || application.blocked.length > 0) && <section className="wb-card pa-result" aria-label="已有计划处理结果">
      {application.continued_run_ids.map((id) => <p key={id}>已自动接续 · <Link className="wb-text-link" to={`/runs/${encodeURIComponent(id)}?view=execution`}>查看执行进展 →</Link></p>)}
      {application.blocked.map((item) => <p key={item.run_id}><strong>暂未继续：</strong>{item.reason} <Link className="wb-text-link" to={`/runs/${encodeURIComponent(item.run_id)}`}>查看这条运行 →</Link></p>)}
    </section>}
    <form className="wb-card pa-policy" id="project-autonomy" onSubmit={savePolicy}>
      <div className="wb-card-head"><div><span className="wb-eyebrow">项目执行方式</span><h2>让 AI 自动推进工作</h2><p>选择一次，后续需求沿用。每次执行记录采用的策略和计划版本。</p></div>{policy && <span className="wb-revision">当前策略 v{policy.revision}</span>}</div>
      <fieldset className="pa-mode-options"><legend>执行模式</legend>
        <label className={`pa-mode ${draft.mode === 'autonomous' ? 'is-selected' : ''}`}><input type="radio" name="execution-mode" value="autonomous" checked={draft.mode === 'autonomous'} onChange={() => setDraft({ ...draft, mode: 'autonomous' })} /><span><strong>Auto · 自动执行</strong><small>目标清楚后自动规划、编写、检查和修复，无需逐项批准计划。</small></span></label>
        <label className={`pa-mode ${draft.mode === 'supervised' ? 'is-selected' : ''}`}><input type="radio" name="execution-mode" value="supervised" checked={draft.mode === 'supervised'} onChange={() => setDraft({ ...draft, mode: 'supervised' })} /><span><strong>监督 · 批准后执行</strong><small>先查看方案，再手动批准这一版计划。</small></span></label>
      </fieldset>
      <div className="pa-explanation"><strong>{draft.mode === 'autonomous' ? '自动推进，过程留档' : '保留人工计划批准'}</strong><p>计划版本、检查结果和代码提交均可追溯。待执行或暂停时可补充要求生成下一版计划；已交付代码可通过 Git 提交修改或回退。</p><p>Auto 沿用已配置的费用与 token 额度；缺少关键需求、超出下方风险范围或检查失败无法修复时，会说明暂停原因。</p></div>
      <div className="wb-policy-grid pa-limits"><label>自动执行的最高风险<select value={draft.max_risk} onChange={(event) => setDraft({ ...draft, max_risk: event.target.value as Policy['max_risk'] })}><option value="low">低 · 常规局部修改</option><option value="medium">中 · 一般工程改动</option><option value="high">高 · 包含高风险任务</option></select></label><label>每项任务最多尝试<input type="number" min={1} max={3} value={draft.max_attempts} onChange={(event) => setDraft({ ...draft, max_attempts: Number(event.target.value) })} /><small>验证失败后，在此次数内自动修复。</small></label></div>
      <div className="wb-policy-toggles"><label className="wb-checkbox"><input type="checkbox" checked={draft.auto_escalate} onChange={(event) => setDraft({ ...draft, auto_escalate: event.target.checked })} />验证失败时按配置升级模型</label><label className="wb-checkbox"><input type="checkbox" checked={draft.resume_on_restart} onChange={(event) => setDraft({ ...draft, resume_on_restart: event.target.checked })} />服务重启后接续尚未派发的工作</label></div>
      {draft.mode === 'autonomous' && <label className="wb-checkbox pa-apply"><input type="checkbox" checked={applyWaiting} onChange={(event) => setApplyWaiting(event.target.checked)} />同时接续本项目正在等待批准的计划</label>}
      <p className="pa-policy-note">{draft.mode === 'autonomous' && applyWaiting ? '保存时会重新核对已有计划的需求、范围、代码基线和预算，通过后直接执行。' : '保存后用于新运行。'} 正在执行的任务保留原策略；超额或失败的旧运行不会自动重启。</p>
      <div className="wb-form-actions"><Link className="wb-text-link" to={`/projects/${encodeURIComponent(projectId)}?tab=settings#project-budget`}>预算与自动交付设置 →</Link><button className="wb-button wb-button-primary" disabled={busy}>{busy ? '保存并处理…' : draft.mode === 'autonomous' ? applyWaiting ? '保存 Auto 并接续计划' : '保存 Auto 模式' : '保存监督模式'}</button></div>
    </form>
    <section className="wb-card"><div className="wb-card-head"><div><span className="wb-eyebrow">Agent 资产</span><h2>项目已绑定能力</h2><p>绑定保存具体版本；编辑能力不会改变现有项目的运行说明。</p></div></div>{bindings.length === 0 ? <div className="wb-empty-inline">这个项目还没有绑定能力。选择一项可调用能力加入项目。</div> : <div className="wb-binding-list">{bindings.map((binding) => <div className="wb-binding-row" key={binding.capability_id}><div><strong>{binding.name}</strong><span>v{binding.revision} · {binding.status === 'ready' ? '可调用' : binding.status}</span></div><button type="button" className="wb-button wb-button-secondary" disabled={busy} onClick={() => void unbind(binding.capability_id)}>移除</button></div>)}</div>}{ready.length > 0 && <div className="wb-bind-control"><label htmlFor="project-capability-version">能力版本</label><select id="project-capability-version" value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">选择可调用能力</option>{ready.map((item) => { const binding = bindings.find((current) => current.capability_id === item.id); const updateLabel = binding && binding.revision !== item.revision ? ' · 更新' : ''; return <option key={item.id} value={item.id}>{item.name} · v{item.revision}{updateLabel}</option> })}</select><button type="button" className="wb-button wb-button-primary" disabled={!selected || busy} onClick={() => void bind()}>{isVersionUpdate ? '更新到此版本' : '绑定版本'}</button></div>}{ready.length === 0 && bindings.length > 0 && <p className="wb-card-foot">没有尚未绑定的可调用能力。可在 <Link className="wb-text-link" to="/capabilities">Agent 能力库</Link> 配置。</p>}</section></div>
}
