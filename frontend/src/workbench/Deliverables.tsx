import { useEffect, useState } from 'react'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
import GithubDelivery, { githubPublicationLabel } from './GithubDelivery'

type Item = { id: number; name: string; kind: string; size: number; sha256: string; origin: string; preview: boolean }
type Catalog = { saved: boolean; can_collect: boolean; github_configured: boolean; github_repository_bound?: boolean; publish_error?: string; collection_error?: string; items: Item[]; note?: string }
const labels: Record<string, string> = { installer: '安装包', package: '压缩包 / 软件包', web: '网页', image: '图片', document: '文档', source: '源码 / 文件' }
function sizeLabel(size: number) { return size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB` }

export default function Deliverables({ run, csrfToken, onUnauthorized, isAdmin }: { run: Run; csrfToken: string; onUnauthorized: () => void; isAdmin: boolean; onPublish?: () => void; publishing?: boolean }) {
  const base = `/api/v3/runs/${encodeURIComponent(String(run.id))}/deliverables`
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [filter, setFilter] = useState('')
  const [preview, setPreview] = useState<{ name: string; content: string; kind: string; image_url?: string } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setCatalog(null); setPreview(null); setError('')
    request<Catalog>(base, { signal: controller.signal, onUnauthorized }).then(async (data) => {
      setCatalog(data)
      const image = data.items.find(item => item.kind === 'image' && item.preview)
      if (image) {
        const content = await request<{ name: string; content: string; kind: string; image_url?: string }>(`${base}/files/${image.id}?preview=true`, { signal: controller.signal, onUnauthorized })
        if (!controller.signal.aborted) setPreview(content)
      }
    }).catch((e) => { if (!controller.signal.aborted) setError(e.message) })
    return () => controller.abort()
  }, [base, run.updated_at, refresh, onUnauthorized])
  async function collect() {
    setBusy(true); setError('')
    try { await request(`${base}/collect`, { method: 'POST', csrfToken, onUnauthorized }); setRefresh(v => v + 1) }
    catch (e) { setError(e instanceof Error ? e.message : '成果保存失败') }
    finally { setBusy(false) }
  }
  async function open(item: Item) {
    setBusy(true); setError(''); setPreview(null)
    try { setPreview(await request(`${base}/files/${item.id}?preview=true`, { onUnauthorized })) }
    catch (e) { setError(e instanceof Error ? e.message : '预览失败') }
    finally { setBusy(false) }
  }
  const items = (catalog?.items ?? []).filter(item => !filter || item.kind === filter)
  return <section className="wb-detail-card wb-deliverables" aria-labelledby="deliverables-title">
    <div className="wb-deliverables-heading"><div><span className="wb-eyebrow">本次成果</span><h2 id="deliverables-title">查看、下载与使用</h2><p>先拿到可用的成果，再选择是否发布到 GitHub。</p></div>{catalog?.saved && <div className="wb-detail-actions"><a className="wb-button wb-button-primary" href={`${base}/download`}>下载全部成果 ZIP</a><GithubDelivery run={run} csrfToken={csrfToken} onUnauthorized={onUnauthorized} isAdmin={isAdmin} configured={catalog.github_configured} /></div>}</div>
    {error && <div role="alert" className="wb-billing-warning">{error}</div>}
    {!catalog && !error && <p role="status">正在读取成果…</p>}
    {catalog && <>
      <div className="wb-notice">成果：{catalog.saved ? `已保存 · ${catalog.items.length} 个文件` : '尚未保存'} · GitHub：{githubPublicationLabel(run, catalog.github_configured, catalog.github_repository_bound)}</div>
      {catalog.publish_error && <p role="alert">{catalog.publish_error}</p>}
      {!catalog.github_configured && <p>如需推送仓库，请联系管理员配置 GitHub 发布凭据；查看和下载成果不依赖 GitHub。</p>}
      {!catalog.saved && <><p>{catalog.collection_error || (catalog.can_collect ? '这次运行尚未归档实际文件，保存后即可预览和下载。' : '执行与验证完成后，系统会自动保存成果。')}</p>{catalog.can_collect && (isAdmin ? <button className="wb-button wb-button-primary" disabled={busy} onClick={() => void collect()}>{busy ? '正在保存…' : '保存本次成果'}</button> : <p>请管理员保存本次成果后即可下载。</p>)}</>}
      {catalog.saved && <>
        <p>{catalog.note}</p>
        {preview && <section className="wb-file-preview" aria-label="成果预览"><div className="wb-deliverables-heading"><h3>{preview.name}</h3><button className="wb-button" onClick={() => setPreview(null)}>关闭预览</button></div>{preview.kind === 'image' && preview.image_url ? <img className="wb-deliverable-image" src={preview.image_url} alt={preview.name} /> : preview.kind === 'web' ? <><p>静态页面预览（脚本与外部资源停用）。完整体验请下载成果后运行。</p><iframe title={preview.name} sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:;">${preview.content}`} /></> : <pre>{preview.content}</pre>}</section>}
        <label>成果类型 <select value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {!items.length && <p>没有此类文件。安装包需要先完成对应平台的构建，系统不会把源码当作安装包。</p>}
        <div className="wb-deliverable-list">{items.map(item => <article className="wb-deliverable-item" key={item.id}><div><strong>{item.name}</strong><p>{labels[item.kind]} · {sizeLabel(item.size)} · {item.origin === 'commit' ? '验收版本' : '构建产物快照'}</p><details><summary>文件校验值</summary><code>{item.sha256}</code></details></div><div className="wb-detail-actions">{item.preview && <button className="wb-button" disabled={busy} onClick={() => void open(item)}>预览</button>}<a className="wb-button" href={`${base}/files/${item.id}`}>下载</a></div></article>)}</div>

        <details><summary>如何提供安装包和其他成果</summary><p>构建结果放在 dist、release 或 out 目录会自动收集。其他文件可在仓库的 .factory-delivery.json 中用 files 列出相对路径，例如：</p><pre>{'{"files": ["packages/app.dmg", "reports/使用说明.pdf"]}'}</pre><p>清单必须提交后参与本次执行。成果会在验证完成后保存；大型文件上限为单个 256 MB、合计 512 MB。</p></details>
      </>}
    </>}
  </section>
}
