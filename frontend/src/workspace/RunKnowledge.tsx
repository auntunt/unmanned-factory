import { useCallback, useEffect, useRef, useState } from 'react'
import { request, WorkspaceApiError } from './api'
import { formatUnknown, type RunContext } from './project-agent-types'
import type { Artifacts, RunStatus } from './types'

interface RunKnowledgeProps {
  runId: string | number
  status: RunStatus | string
  artifacts?: Artifacts | null
  revision?: number
  csrfToken: string
  onUnauthorized: () => void
}

function isAbort(cause: unknown): boolean { return cause instanceof DOMException && cause.name === 'AbortError' }
function errorText(cause: unknown): string { return cause instanceof WorkspaceApiError ? cause.detail : cause instanceof Error ? cause.message : '请求失败' }
function dateText(value: unknown): string { if (typeof value !== 'string') return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false }) }
function isSafeGithubUrl(value: string): boolean { try { const url = new URL(value); return url.protocol === 'https:' && url.hostname === 'github.com' && /^\/[^/]+\/[^/]+\/pull\/\d+(?:\/)?$/.test(url.pathname) } catch { return false } }

interface RunKnowledgeViewProps {
  status: RunStatus | string
  artifacts?: Artifacts | null
  context: RunContext | null
  loading: boolean
  error: string | null
  mergeBusy: boolean
  mergeResult: Record<string, unknown> | null
  onSyncMerge: () => void
}

export function RunKnowledgeView({ status, artifacts, context, loading, error, mergeBusy, mergeResult, onSyncMerge }: RunKnowledgeViewProps) {
  const prUrl = typeof artifacts?.pr_url === 'string' && artifacts.pr_url.length > 0 ? artifacts.pr_url : null
  const warnings = Array.isArray(context?.warnings) ? context.warnings : []
  const code = context?.code && typeof context.code === 'object' && !Array.isArray(context.code) ? context.code as Record<string, unknown> : null
  const evidence = mergeResult?.evidence && typeof mergeResult.evidence === 'object' && !Array.isArray(mergeResult.evidence) ? mergeResult.evidence as Record<string, unknown> : null
  const mergeSha = evidence?.merge_commit_sha
  return <section className="pa-panel rk-root">
    <div className="pa-panel-heading"><div><h2>运行知识上下文</h2><p>这是运行启动时冻结的证据快照，不会随项目后续更新而改变。</p></div><span className="pa-tag">状态：{status}</span></div>
    {error && <div className="pa-error" role="alert">{error}</div>}
    {loading ? <div className="pa-loading">正在读取冻结上下文…</div> : context === null ? <div className="pa-empty">该运行没有保存的上下文快照。</div> : <div className="rk-context"><div className="rk-meta"><span>组装时间：{dateText(context.assembled_at ?? context.created_at)}</span><span>基线 SHA：{typeof context.commit_sha === 'string' ? context.commit_sha : '—'}</span><span>索引 SHA：{typeof code?.commit_sha === 'string' ? code.commit_sha : '—'}</span></div>{warnings.length > 0 && <div className="pa-warning"><strong>上下文警告</strong>{warnings.map((warning, index) => <div key={`${String(warning)}-${index}`}>{String(warning)}</div>)}</div>}<pre className="rk-snapshot">{formatUnknown(context)}</pre></div>}
    {prUrl && <div className="rk-merge"><div><h3>合并确认</h3><p>已记录 PR：{isSafeGithubUrl(prUrl) ? <a href={prUrl} target="_blank" rel="noreferrer">{prUrl}</a> : <span>{prUrl}</span>}。确认会重新向 GitHub 校验，不会执行远程写操作。</p></div><button className="wf-button wf-button-primary" disabled={mergeBusy} onClick={onSyncMerge}>{mergeBusy ? '核验中…' : '核验合并状态'}</button></div>}
    {mergeResult && <div className={mergeResult.merged === true ? 'pa-success' : 'pa-info'} role="status">{mergeResult.merged === true ? `已核验并记录合并事实（${String(mergeSha ?? 'merge SHA 未返回')}）。` : `尚未合并：${String(mergeResult.reason ?? 'GitHub 尚未返回 merged=true')}。`}</div>}
  </section>
}

export default function RunKnowledge({ runId, status, artifacts, revision, csrfToken, onUnauthorized }: RunKnowledgeProps) {
  const [context, setContext] = useState<RunContext | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [mergeBusy, setMergeBusy] = useState(false)
  const [mergeResult, setMergeResult] = useState<Record<string, unknown> | null>(null)
  const previousRunId = useRef<string | number>(runId)
  const mergeController = useRef<AbortController | null>(null)
  const mounted = useRef(false)

  const load = useCallback(async (signal: AbortSignal) => {
    setLoading(true); setError(null)
    try { const next = await request<RunContext | null>(`/api/v2/runs/${encodeURIComponent(String(runId))}/context`, { onUnauthorized, signal }); if (!signal.aborted) setContext(next) } catch (cause) { if (!isAbort(cause) && !signal.aborted) setError(errorText(cause)) } finally { if (!signal.aborted) setLoading(false) }
  }, [onUnauthorized, runId])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; mergeController.current?.abort() } }, [])
  useEffect(() => {
    const isNewRun = String(previousRunId.current) !== String(runId)
    if (isNewRun) { previousRunId.current = runId; setContext(null); setMergeResult(null); setMergeBusy(false) }
    const controller = new AbortController(); void load(controller.signal)
    return () => controller.abort()
  }, [load, revision, runId, status])

  const syncMerge = async () => {
    mergeController.current?.abort(); const controller = new AbortController(); mergeController.current = controller; setMergeBusy(true); setError(null)
    try { const next = await request<Record<string, unknown>>(`/api/v2/runs/${encodeURIComponent(String(runId))}/sync-merge`, { method: 'POST', csrfToken, onUnauthorized, signal: controller.signal }); if (!controller.signal.aborted && mounted.current) setMergeResult(next) } catch (cause) { if (!isAbort(cause) && !controller.signal.aborted && mounted.current && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { if (!controller.signal.aborted && mounted.current && mergeController.current === controller) setMergeBusy(false) }
  }

  return <RunKnowledgeView status={status} artifacts={artifacts} context={context} loading={loading} error={error} mergeBusy={mergeBusy} mergeResult={mergeResult} onSyncMerge={() => void syncMerge()} />
}

export { RunKnowledge }
