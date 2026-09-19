import Icon from './Icon'
import { useEffect, useState } from 'react'
import { request } from '../workspace/api'
import type { Run, CapabilitySources, ServiceUrl } from '../workspace/types'
import GithubDelivery, { githubPublicationLabel } from './GithubDelivery'
import { DELIVERY_TYPE_LABEL, type DeliveryType } from '../conversation/delivery-constants'
import CapabilitySourcePanel from './CapabilitySourcePanel'
import PackFromDeliverables from './PackFromDeliverables'

type Item = { id: number; name: string; kind: string; size: number; sha256: string; origin: string; preview: boolean }
type Catalog = { recommended_preview_id?: number | null; saved: boolean; can_collect: boolean; github_configured: boolean; github_repository_bound?: boolean; publish_error?: string; collection_error?: string; items: Item[]; note?: string; delivery_type?: DeliveryType; installer_targets?: string[] | null; capability_sources?: CapabilitySources; service_urls?: ServiceUrl[] }
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
      const image = data.items.find(item => item.id === data.recommended_preview_id && item.preview)
        ?? data.items.find(item => item.kind === 'image' && item.preview)
        ?? data.items.find(item => /^(dist|out|release)\/index\.html$/.test(item.name) && item.preview)
        ?? data.items.find(item => /(^|\/)readme\.md$/i.test(item.name) && item.preview)
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
  const primary = [...(catalog?.items ?? [])].sort((a,b) => Number(!/(^|\/)(readme|使用说明)/i.test(a.name)) - Number(!/(^|\/)(readme|使用说明)/i.test(b.name))).filter(item => item.preview && (item.kind === 'web' || item.kind === 'image' || /(^|\/)(readme|使用说明)/i.test(item.name))).slice(0, 6)
  const items = (catalog?.items ?? []).filter(item => !filter || item.kind === filter)
  return <section className="wb-detail-card wb-deliverables" aria-labelledby="deliverables-title">
    <div className="wb-deliverables-heading"><div><span className="wb-eyebrow">本次成果</span><h2 id="deliverables-title">查看、下载与使用</h2><p>先拿到可用的成果，再选择是否发布到 GitHub。</p></div>{catalog?.saved && <div className="wb-detail-actions"><a className="wb-button wb-button-primary" href={`${base}/download`}>下载全部成果 ZIP</a><GithubDelivery run={run} csrfToken={csrfToken} onUnauthorized={onUnauthorized} isAdmin={isAdmin} configured={catalog.github_configured} /></div>}</div>
    {error && <div role="alert" className="wb-billing-warning">{error}</div>}
    {!catalog && !error && <p role="status">正在读取成果…</p>}
    {catalog && <>
      <div className="wb-notice">成果：{catalog.saved ? `已保存 · ${catalog.items.length} 个文件` : '尚未保存'} · GitHub：{githubPublicationLabel(run, catalog.github_configured, catalog.github_repository_bound)}{catalog.delivery_type ? ` · 交付类型：${DELIVERY_TYPE_LABEL[catalog.delivery_type] || catalog.delivery_type}` : ' · 交付类型：未声明'}</div>
      <DeliverableDeliverySection deliveryType={catalog.delivery_type ?? null} installerTargets={catalog.installer_targets ?? null} run={run} serviceUrls={catalog.service_urls ?? []} />
      {catalog.publish_error && <p role="alert">{catalog.publish_error}</p>}
      {!catalog.github_configured && <p>如需推送仓库，请联系管理员配置 GitHub 发布凭据；查看和下载成果不依赖 GitHub。</p>}
      {!catalog.saved && <><p>{catalog.collection_error || (catalog.can_collect ? '这次运行尚未归档实际文件，保存后即可预览和下载。' : '执行与验证完成后，系统会自动保存成果。')}</p>{catalog.can_collect && (isAdmin ? <button className="wb-button wb-button-primary" disabled={busy} onClick={() => void collect()}>{busy ? '正在保存…' : '保存本次成果'}</button> : <p>请管理员保存本次成果后即可下载。</p>)}</>}
      {catalog.saved && <>
        <p>{catalog.note}</p>
        <div className="wb-delivery-entrypoints" aria-label="快速查看成果">{primary.map(item => <button key={item.id} className="wb-button wb-button-secondary" disabled={busy} onClick={() => void open(item)}>{item.kind === 'web' ? '查看页面' : item.kind === 'image' ? '查看截图 / 图片' : '阅读使用说明'} <small>{item.name}</small></button>)}</div>
        {preview && <section className="wb-file-preview" aria-label="成果预览"><div className="wb-deliverables-heading"><h3>{preview.name}</h3><button className="wb-button" onClick={() => setPreview(null)}>关闭预览</button></div>{preview.kind === 'image' && preview.image_url ? <img className="wb-deliverable-image" src={preview.image_url} alt={preview.name} /> : preview.kind === 'web' ? <><p>页面外观预览：加载归档中的样式与图片，脚本不运行。若页面由脚本生成，请查看截图；完整交互请下载后按说明启动。</p><iframe title={preview.name} sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:;">${preview.content}`} /></> : <pre>{preview.content}</pre>}</section>}
        <details className="wb-delivery-files"><summary><Icon name="triangle" className="wb-disclosure-icon" />全部文件与下载 · {catalog.items.length} 个文件</summary><label>成果类型 <select value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {!items.length && <p>没有此类文件。安装包需要先完成对应平台的构建，系统不会把源码当作安装包。</p>}
        <div className="wb-deliverable-list">{items.map(item => <article className="wb-deliverable-item" key={item.id}><div><strong>{item.name}</strong><p>{labels[item.kind]} · {sizeLabel(item.size)} · {item.origin === 'commit' ? '验收版本' : '构建产物快照'}</p><details><summary><Icon name="triangle" className="wb-disclosure-icon" />文件校验值</summary><code>{item.sha256}</code></details></div><div className="wb-detail-actions">{item.preview && <button className="wb-button" disabled={busy} onClick={() => void open(item)}>预览</button>}<a className="wb-button" href={`${base}/files/${item.id}`}>下载</a></div></article>)}</div>

        </details>
        <details><summary><Icon name="triangle" className="wb-disclosure-icon" />如何提供安装包和其他成果</summary><p>构建结果放在 dist、release 或 out 目录会自动收集。其他文件可在仓库的 .factory-delivery.json 中用 files 列出相对路径，例如：</p><pre>{'{"files": ["packages/app.dmg", "reports/使用说明.pdf"]}'}</pre><p>清单必须提交后参与本次执行。成果会在验证完成后保存；大型文件上限为单个 256 MB、合计 512 MB。</p></details>
      </>}
      {catalog.capability_sources && <CapabilitySourcePanel sources={catalog.capability_sources} />}
      {isAdmin && catalog.saved && <PackFromDeliverables key={String(run.id)} runId={String(run.id)} files={catalog.items} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />}
      <ContinueModify run={run} csrfToken={csrfToken} onUnauthorized={onUnauthorized} />
    </>}
  </section>
}

/** Delivery-type-specific status, consistent with RunWorkspace's DeliverySection.
 *  Uses the same shared DELIVERY_TYPE_LABEL constant to guarantee label parity. */
function DeliverableDeliverySection({ deliveryType, installerTargets, run, serviceUrls }: { deliveryType: DeliveryType; installerTargets: string[] | null; run: Run; serviceUrls: ServiceUrl[] }) {
  const deployed = run.status === 'published'
  if (deliveryType === 'cli') {
    return <div className="wb-delivery-type-section" data-delivery-type="cli">
      <p><strong>部署：</strong><span>不适用</span> — 命令行工具通过下载使用，无需在线部署。</p>
    </div>
  }
  if (deliveryType === 'installer') {
    return <div className="wb-delivery-type-section" data-delivery-type="installer">
      <p><strong>安装目标：</strong>{installerTargets && installerTargets.length > 0
        ? <span>{installerTargets.join('、')}</span>
        : <span>待确认</span>}</p>
      {!installerTargets && <p>尚未选定目标系统，安装包构建暂不可用。</p>}
    </div>
  }
  if (deliveryType === 'service') {
    return <div className="wb-delivery-type-section" data-delivery-type="service">
      <p><strong>部署：</strong>{deployed ? <span>已部署</span> : <span>待部署</span>}</p>
      {serviceUrls.length > 0 ? (
        <div data-testid="service-urls">
          <p><strong>固定测试地址：</strong></p>
          <ul>{serviceUrls.map(s => (
            <li key={s.url}><a href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</a>{s.name ? ` (${s.name})` : ''}</li>
          ))}</ul>
        </div>
      ) : (
        <p data-testid="no-service-url">未登记固定地址，请管理员补齐。</p>
      )}
    </div>
  }
  return <div className="wb-delivery-type-section" data-delivery-type="undeclared">
    <p><strong>交付类型：</strong><span>未声明</span></p>
  </div>
}

/** "继续修改" entry in the delivery view.
 *  Submits a followup note via the existing notes API (POST /api/v2/runs/{rid}/notes). */
function ContinueModify({ run, csrfToken, onUnauthorized }: { run: Run; csrfToken: string; onUnauthorized: () => void }) {
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!content.trim() || busy) return
    setBusy(true); setError('')
    try {
      await request(`/api/v2/runs/${encodeURIComponent(String(run.id))}/notes`, {
        method: 'POST', csrfToken, onUnauthorized,
        body: { content: content.trim(), idempotency_key: crypto.randomUUID() },
      })
      setSent(true); setContent('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '提交失败')
    } finally { setBusy(false) }
  }

  return (
    <details className="wb-continue-modify" data-testid="continue-modify">
      <summary><Icon name="triangle" className="wb-disclosure-icon" />继续修改</summary>
      {sent ? (
        <p>修改要求已提交，系统将基于当前成果继续处理。</p>
      ) : (
        <form onSubmit={e => void submit(e)}>
          <p>描述需要继续修改的内容，系统会基于当前版本和项目设置继续处理。</p>
          <div className="wb-field">
            <label htmlFor="continue-modify-content">修改内容</label>
            <textarea id="continue-modify-content" value={content} onChange={e => setContent(e.target.value)} placeholder="说明需要修改的部分…" />
          </div>
          <button className="wb-button wb-button-primary" disabled={busy || !content.trim()} type="submit">{busy ? '正在提交…' : '提交修改要求'}</button>
          {error && <p role="alert" className="wb-billing-warning">{error}</p>}
        </form>
      )}
    </details>
  )
}
