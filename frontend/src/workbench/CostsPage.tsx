import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText, type PageProps } from './ui'
import type { OverviewData } from './v3-types'

const tokenFormat = new Intl.NumberFormat('zh-CN')

function tokens(value: number, knownCalls: number) {
  return knownCalls > 0 ? tokenFormat.format(value) : '未记录'
}

export default function CostsPage({ onUnauthorized }: PageProps) {
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
    <PageHeader title="调用记录" description="查看模型、token 和提示缓存是否真正命中。计费与 token 额度由中转站统一管理。" actions={<><button className="wb-button wb-button-secondary" onClick={() => setRefreshIndex((value) => value + 1)}>刷新</button><Link className="wb-button wb-button-secondary" to="/settings/runtime">运行配置</Link></>} />
    {error && <ErrorNotice message={error} />}
    {!data && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>}
    {data && <>
      <div className="wb-cost-summary">
        <article><span>运行总数</span><strong>{data.runs}</strong><small>包含规划与执行</small></article>
        <article><span>模型调用</span><strong>{calls}</strong><small>{tokenCalls > 0 ? `${tokenCalls} 次带 token 数据` : '尚无 token 数据'}</small></article>
        <article><span>提示缓存</span><strong>{cacheCalls === 0 ? '未知' : cacheReads > 0 ? '已命中' : '未命中'}</strong><small>{cacheMessage}</small></article>
      </div>
      <section className="wb-card wb-cost-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">真实调用记录</span><h2>按角色查看</h2><p>缓存读取和缓存创建分开统计；创建缓存不算命中。旧版无法区分的 Claude 数据不参与缓存统计。</p></div></div>
        {data.model_usage.length === 0
          ? <EmptyState title="还没有调用记录" description="运行产生调用记录后，这里会显示角色、provider、模型和 token。" />
          : <div className="wb-table-wrap"><table className="wb-table"><thead><tr><th>角色</th><th>Provider</th><th>模型</th><th>调用</th><th>输入 token</th><th>输出 token</th><th>缓存创建</th><th>缓存读取</th></tr></thead><tbody>{data.model_usage.map((item, index) => <tr key={`${item.profile}-${item.provider ?? 'unknown'}-${item.model ?? 'unknown'}-${index}`}><td><strong>{item.profile}</strong></td><td className="wb-mono">{item.provider || '未记录'}</td><td className="wb-mono">{item.model || '未配置'}</td><td>{item.calls}</td><td>{tokens(item.input_tokens, item.token_usage_calls)}</td><td>{tokens(item.output_tokens, item.token_usage_calls)}</td><td>{tokens(item.cache_creation_input_tokens, item.cache_usage_calls)}</td><td>{item.cache_usage_calls > 0 && item.cached_input_tokens === 0 ? '0（未命中）' : tokens(item.cached_input_tokens, item.cache_usage_calls)}</td></tr>)}</tbody></table></div>}
      </section>
      <section className="wb-card wb-cost-note"><strong>缓存诊断</strong><span>{cacheMessage} 这里显示的是模型提示缓存；npm、pip、uv 的依赖缓存由执行环境按项目独立复用。</span></section>
      <section className="wb-card wb-cost-note"><strong>计费管理</strong><span>账户计费和 token 额度由中转站管理；webuddy 的项目预算只用于在达到上限后阻止后续模型调用。</span></section>
    </>}
  </div>
}
