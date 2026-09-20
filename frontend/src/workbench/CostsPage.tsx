import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText, type PageProps } from './ui'
import type { CostPolicy, OverviewData } from './v3-types'

const tokenFormat = new Intl.NumberFormat('zh-CN')

function tokens(value: number, knownCalls: number) {
  return knownCalls > 0 ? tokenFormat.format(value) : '—'
}

// The one admin entry for the platform cost policy. It lives on the existing
// cost page rather than a new top-level nav item, and its default stays
// "monitor only": an unconfigured policy must never add a hidden hard stop.
function CostPolicyEditor({ policy, csrfToken, onUnauthorized, onSaved }: PageProps & { policy: CostPolicy; onSaved: (next: CostPolicy) => void }) {
  const [enforce, setEnforce] = useState(policy.default_project_budget_usd != null)
  const [amount, setAmount] = useState(policy.default_project_budget_usd == null ? '' : String(policy.default_project_budget_usd))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dirtyRef = useRef(false)
  useEffect(() => { if (dirtyRef.current) return; setEnforce(policy.default_project_budget_usd != null); setAmount(policy.default_project_budget_usd == null ? '' : String(policy.default_project_budget_usd)) }, [policy])
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError(null); setSaved(false)
    const value = enforce ? Number(amount) : null
    if (value !== null && (!Number.isFinite(value) || value <= 0 || value > 1000000)) { setError('默认停止线需要是 0 到 1000000 之间的数字。'); return }
    setBusy(true)
    try {
      const next = await request<CostPolicy>('/api/v2/runtime/cost-policy', { method: 'PUT', csrfToken, onUnauthorized, body: { revision: policy.revision, default_project_budget_usd: value } })
      dirtyRef.current = false; onSaved(next); setSaved(true)
    } catch (cause) { setError(errorText(cause)) } finally { setBusy(false) }
  }
  return <form className="wb-card wb-form" onSubmit={save} aria-label="平台费用策略">
    <div className="wb-card-head"><div><span className="wb-eyebrow">平台费用策略</span><h2>新项目的默认费用模式</h2><p>只影响此后新建、且仍跟随平台策略的项目。已有项目的设置和现有停止线不会被改写。</p></div><span className="wb-revision">修订 {policy.revision}</span></div>
    <label>默认模式<select value={enforce ? 'enforce' : 'monitor'} disabled={busy} onChange={(event) => { dirtyRef.current = true; setEnforce(event.target.value === 'enforce'); setSaved(false) }}><option value="monitor">仅监测（默认，不因美元金额停止）</option><option value="enforce">给新项目设默认停止线</option></select></label>
    {enforce && <label htmlFor="cost-policy-default">默认单次运行停止线（美元）<input id="cost-policy-default" type="number" min="0.01" max="1000000" step="0.01" required value={amount} disabled={busy} onChange={(event) => { dirtyRef.current = true; setAmount(event.target.value); setSaved(false) }} /></label>}
    <small>{enforce ? `跟随策略的项目每条需求累计到 $${amount || '—'} 后停止派发新的模型调用；项目设置里可以为单个项目单独指定。` : '不配置策略时，新项目只记录费用和 token，不设美元停止线。'}</small>
    {error && <ErrorNotice message={error} />}
    <div className="wb-form-actions"><button className="wb-button wb-button-primary" disabled={busy}>{busy ? '保存中…' : '保存费用策略'}</button>{saved && <span role="status">已保存。</span>}</div>
  </form>
}

export default function CostsPage({ onUnauthorized, csrfToken, user }: PageProps) {
  const isAdmin = user?.role === 'admin'
  const [data, setData] = useState<OverviewData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshIndex, setRefreshIndex] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    void request<OverviewData>('/api/v3/overview', { onUnauthorized, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setData(value) })
      .catch((cause) => {
        if (!controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause))
      })
    const timer = window.setInterval(() => setRefreshIndex((value) => value + 1), 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [onUnauthorized, refreshIndex])

  const calls = data?.model_usage.reduce((sum, item) => sum + item.calls, 0) ?? 0
  const tokenCalls = data?.model_usage.reduce((sum, item) => sum + item.token_usage_calls, 0) ?? 0
  const cacheCalls = data?.model_usage.reduce((sum, item) => sum + item.cache_usage_calls, 0) ?? 0
  const cacheReads = data?.model_usage.reduce((sum, item) => sum + item.cached_input_tokens, 0) ?? 0
  const cacheMessage = cacheCalls === 0
    ? '中转站尚未返回可核对的缓存读写数据。'
    : cacheReads === 0
      ? `已核对 ${cacheCalls} 次调用，缓存读取均为 0；当前链路没有产生缓存命中。`
      : `已观察到 ${tokenFormat.format(cacheReads)} 个缓存读取 token。`

  return <div className="wb-page">
    <PageHeader title="用量与预算" description="监测模型费用和 token；项目停止线与团队月度额度分别管理。供应商未返回的费用不会视为零。" actions={<><button className="wb-button wb-button-secondary" onClick={() => setRefreshIndex((value) => value + 1)}>刷新</button><Link className="wb-button wb-button-secondary" to="/settings/runtime">运行配置</Link></>} />
    {error && <ErrorNotice message={error} />}
    {!data && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {data && <>
      <div className="wb-cost-summary">
        <article><span>已报告费用（美元）</span><strong>{typeof data.known_cost_usd === 'number' ? `$${data.known_cost_usd.toFixed(4)}` : '费用未知'}</strong><small>{data.unknown_cost_runs ?? '未知数量的'} 条运行费用不完整</small></article>
        <article><span>运行总数</span><strong>{data.runs}</strong><small>包含规划与执行</small></article>
        <article><span>模型调用</span><strong>{calls}</strong><small>{tokenCalls > 0 ? `${tokenCalls} 次带 token 数据` : '尚无 token 数据'}</small></article>
        <article><span>提示缓存</span><strong title={cacheMessage} tabIndex={0} aria-label={`提示缓存：${cacheMessage}`}>{cacheCalls === 0 ? '未知' : cacheReads > 0 ? '已命中' : '未命中'}</strong></article>
      </div>
      <section className="wb-card wb-cost-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">真实调用记录</span><h2>按角色查看</h2><p>缓存读取和缓存创建分开统计；创建缓存不算命中。旧版无法区分的 Claude 数据不参与缓存统计。</p></div></div>
        {data.model_usage.length === 0
          ? <EmptyState title="还没有调用记录" description="运行产生调用记录后，这里会显示角色、provider、模型和 token。" />
          : <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>角色</th><th>Provider</th><th>模型</th><th>调用</th><th>输入 token</th><th>输出 token</th><th>缓存创建</th><th>缓存读取</th></tr></thead><tbody>{data.model_usage.map((item, index) => <tr key={`${item.profile}-${item.provider ?? 'unknown'}-${item.model ?? 'unknown'}-${index}`}><td><strong>{item.profile}</strong></td><td className="wb-mono">{item.provider || '—'}</td><td className="wb-mono">{item.model || '未配置'}</td><td>{item.calls}</td>{item.token_usage_calls === 0 && item.cache_usage_calls === 0 ? <td colSpan={4}><span className="wb-mode-badge">供应商未返回明细</span></td> : <><td>{tokens(item.input_tokens, item.token_usage_calls)}</td><td>{tokens(item.output_tokens, item.token_usage_calls)}</td><td>{tokens(item.cache_creation_input_tokens, item.cache_usage_calls)}</td><td>{item.cache_usage_calls > 0 && item.cached_input_tokens === 0 ? '0（未命中）' : tokens(item.cached_input_tokens, item.cache_usage_calls)}</td></>}</tr>)}</tbody></table></div>}
      </section>
      <section className="wb-card wb-cost-note"><strong>缓存诊断</strong><span>{cacheMessage} 这里显示的是模型提示缓存；npm、pip、uv 的依赖缓存由执行环境按项目独立复用。</span></section>
      {isAdmin && data.cost_policy && <CostPolicyEditor policy={data.cost_policy} csrfToken={csrfToken} onUnauthorized={onUnauthorized} user={user} onSaved={(next) => setData((current) => current ? { ...current, cost_policy: next } : current)} />}
      <section className="wb-card"><h2>项目费用控制</h2><p>仅监测不会按美元金额暂停；已配置停止线的项目保留原值，可在项目设置中关闭。「跟随平台策略」的项目随上面的默认模式变化，「项目单独指定」的不受它影响。</p><div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>项目</th><th>当前生效</th><th>来源</th><th>配置</th></tr></thead><tbody>{data.project_summaries?.map(project => <tr key={project.id}><td>{project.name}</td><td>{(project.effective_budget_usd ?? project.budget_usd) == null ? '仅监测' : `每次运行 $${project.effective_budget_usd ?? project.budget_usd} 后停止`}</td><td>{project.budget_source === 'inherit' ? '跟随平台策略' : '项目单独指定'}</td><td><Link to={`/projects/${encodeURIComponent(project.id)}?tab=settings#project-budget`}>管理 {project.name} 预算</Link></td></tr>)}</tbody></table></div><Link to="/team">团队 token 用量与额度（留空不限额）</Link></section>
    </>}
  </div>
}
