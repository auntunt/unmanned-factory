import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import type { Run } from '../workspace/types'
import { request } from '../workspace/api'
import { notifyDataChanged } from './data-refresh'

export interface GithubOptions {
  github_configured: boolean
  github_repository?: string | null
  github_repository_bound: boolean
  page: number
  has_more: boolean
  project_revision: number
  repositories: Array<{ full_name: string; private?: boolean }>
}

export function githubPublicationLabel(run: Run, configured: boolean, bound?: boolean): string {
  if (run.status === 'published') return run.artifacts?.publication_type === 'initial' ? (typeof run.artifacts.published_branch === 'string' && run.artifacts.published_branch ? `已上传到仓库 ${run.artifacts.published_branch} 分支` : '已首次上传到仓库') : run.artifacts?.pr_url ? '已创建 PR' : '已上传'
  if (run.status === 'publishing') return '正在上传'
  if (!configured) return '未配置发布凭据'
  return bound === false ? '尚未绑定仓库' : '尚未上传'
}

export function safeRepositoryUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try { const url = new URL(value); return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port && !/^https:\/\/[^/]*:[0-9]+(?:\/|$)/i.test(value) && /^\/[^/]+\/[^/]+\/?$/.test(url.pathname) ? url.href : null } catch { return null }
}

export function githubPublishBody(mode: 'bound' | 'existing' | 'create', options: GithubOptions, repository: string, name: string) {
  if (mode === 'existing' && !repository) throw new Error('请选择要上传的仓库。')
  if (mode === 'create' && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(name.trim())) throw new Error('仓库名称请以英文字母或数字开头，共 1–100 个字母、数字、点、短横线或下划线。')
  return { mode, expected_project_revision: options.project_revision, ...(mode === 'existing' ? { repository } : {}), ...(mode === 'create' ? { name: name.trim(), private: true } : {}) }
}

export default function GithubDelivery({ run, csrfToken, onUnauthorized, isAdmin, configured }: { run: Run; csrfToken: string; onUnauthorized: () => void; isAdmin: boolean; configured: boolean }) {
  const [open, setOpen] = useState(false)
  const [options, setOptions] = useState<GithubOptions | null>(null)
  const [mode, setMode] = useState<'bound' | 'existing' | 'create'>('create')
  const [repository, setRepository] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [published, setPublished] = useState<Run | null>(null)
  const [refresh, setRefresh] = useState(0)
  const api = `/api/v3/runs/${encodeURIComponent(String(run.id))}`
  useEffect(() => {
    setPublished(null); setOpen(false); setOptions(null)
  }, [run.id])
  useEffect(() => {
    if (!open || !isAdmin) return
    const controller = new AbortController()
    setOptions(null); setError('')
    void request<GithubOptions>(`${api}/github-options`, { signal: controller.signal, onUnauthorized }).then(value => {
      if (!controller.signal.aborted) { setOptions(value); setMode(value.github_repository_bound ? 'bound' : 'create') }
    }).catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '无法读取仓库列表') })
    return () => controller.abort()
  }, [api, open, isAdmin, onUnauthorized, refresh])
  async function moreRepositories() {
    if (!options || busy) return
    setBusy(true); setError('')
    try {
      const next = await request<GithubOptions>(`${api}/github-options?page=${options.page + 1}`, { onUnauthorized })
      setOptions(current => current ? { ...current, page: next.page, has_more: next.has_more, repositories: [...new Map([...current.repositories, ...next.repositories].map(item => [item.full_name, item])).values()] } : next)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取更多仓库') }
    finally { setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!options || busy) return
    setError('')
    try {
      const body = githubPublishBody(mode, options, repository, name)
      setBusy(true)
      const result = await request<Run>(`${api}/github-publish`, { method: 'POST', csrfToken, onUnauthorized, body })
      setPublished(result); setOpen(false); notifyDataChanged()
    } catch (cause) { setError(cause instanceof Error ? cause.message : '上传失败，请重试') }
    finally { setBusy(false) }
  }
  const displayedRun = published ?? run
  const link = safeRepositoryUrl(displayedRun.artifacts?.repository_url)
  if (run.status === 'published' || published) return <span role="status">{githubPublicationLabel(displayedRun, configured)} {link && <a className="wb-button" target="_blank" rel="noreferrer" href={link}>打开仓库 ↗</a>}</span>
  return <div>
    {isAdmin ? <button type="button" className="wb-button wb-button-primary" onClick={() => setOpen(value => !value)} disabled={busy || run.status === 'publishing' || run.status !== 'ready_for_review'}>{run.status === 'publishing' ? '正在上传 GitHub…' : open ? '收起 GitHub 选项' : '上传到 GitHub'}</button> : <span>{configured ? '请管理员选择仓库并上传' : 'GitHub 未配置发布凭据'}</span>}
    {open && <section className="wb-policy-snapshot" aria-label="GitHub 上传选项"><h3>上传成果到 GitHub</h3>{!options && !error && <p role="status">正在读取可用仓库…</p>}{error && <p role="alert">{error}</p>}{options && !options.github_configured && <p>尚未配置 GitHub 发布凭据，请管理员在服务器端配置后重试。</p>}{options?.github_configured && <form className="wb-form" onSubmit={submit}><p>{options.github_repository_bound ? `当前绑定：${options.github_repository}` : '尚未绑定仓库。可以选择已有仓库，或创建私有仓库。'}</p><label>上传位置<select disabled={busy} value={mode} onChange={event => setMode(event.target.value as typeof mode)}>{options.github_repository_bound && <option value="bound">使用当前绑定仓库</option>}<option value="create">创建私有仓库</option><option value="existing">选择已有仓库</option></select></label>{mode === 'existing' && <label>已有仓库<select required disabled={busy} value={repository} onChange={event => setRepository(event.target.value)}><option value="">请选择仓库</option>{options.repositories.map(item => <option key={item.full_name} value={item.full_name}>{item.full_name}{item.private ? ' · 私有' : ' · 公开'}</option>)}</select></label>}{mode === 'existing' && options.has_more && <button type="button" className="wb-button" disabled={busy} onClick={() => void moreRepositories()}>加载更多仓库</button>}{mode === 'create' && <label>新仓库名称<input required disabled={busy} maxLength={100} value={name} onChange={event => setName(event.target.value)} placeholder="例如 work-hours-tool" /><small>默认创建私有仓库，首次上传到 main 分支。</small></label>}<button className="wb-button wb-button-primary" disabled={busy}>{busy ? '正在上传…' : mode === 'create' ? '创建私有仓库并上传' : '上传成果'}</button></form>}<button type="button" className="wb-button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新仓库与配置</button></section>}
  </div>
}
