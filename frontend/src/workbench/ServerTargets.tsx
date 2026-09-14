import { useEffect, useState, type FormEvent } from 'react'
import { request } from '../workspace/api'
import { ErrorNotice, errorText } from './ui'

type Props = { csrfToken: string; onUnauthorized: () => void }
export interface ServerTarget { id: string; name: string; host: string; port: number; user: string; host_fingerprint: string; public_key: string; public_fingerprint: string; commands: Record<string, string>; revision: number }
const labels = { health_check: '健康检查命令或 URL', service_status: '服务状态命令', fetch_log: '日志绝对路径', deploy: '部署脚本命令', rollback: '回滚脚本命令' }
const blank = { name: '', host: '', port: 22, user: '', host_fingerprint: '', commands: {} as Record<string, string> }
const url = '/api/v2/deploy-targets'

export default function ServerTargets({ csrfToken, onUnauthorized }: Props) {
  const [targets, setTargets] = useState<ServerTarget[]>([])
  const [editing, setEditing] = useState<ServerTarget | null>(null)
  const [draft, setDraft] = useState(blank)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const load = async () => setTargets((await request<{ targets: ServerTarget[] }>(url, { onUnauthorized })).targets)
  useEffect(() => { const controller = new AbortController(); void request<{ targets: ServerTarget[] }>(url, { onUnauthorized, signal: controller.signal }).then(r => setTargets(r.targets)).catch(e => { if (!controller.signal.aborted) setError(errorText(e)) }); return () => controller.abort() }, [onUnauthorized])
  const act = async (action: () => Promise<void>) => { if (busy) return; setBusy(true); setError(null); setNotice(''); try { await action() } catch (e) { setError(errorText(e)) } finally { setBusy(false) } }
  const save = (event: FormEvent) => { event.preventDefault(); void act(async () => { await request(url + (editing ? `/${editing.id}` : ''), { method: editing ? 'PUT' : 'POST', csrfToken, onUnauthorized, body: { ...draft, ...(editing ? { revision: editing.revision } : {}) } }); setEditing(null); setDraft(blank); await load(); setNotice('目标已保存。新目标请先布置公钥，再测试连接并绑定项目。') }) }
  return <section className="wb-detail-card wb-runtime-section" aria-label="服务器连接"><h2>服务器连接</h2><p>每个目标使用独立公钥，只执行预先登记的动作。请从服务器管理员处核对主机 SHA256 指纹；私钥不会显示或下载。</p>
    {targets.map(target => <article className="wb-check-detail" key={target.id}><h3>{target.name}</h3><p>{target.user}@{target.host}:{target.port}</p><p>部署公钥指纹：{target.public_fingerprint}</p><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{target.public_key}</pre><p>将此公钥加入目标服务器专用用户的 ~/.ssh/authorized_keys，限制其权限后测试连接。部署脚本须由服务器管理员自行维护。</p><div className="wb-form-actions"><button type="button" disabled={busy} onClick={() => void act(async () => { await navigator.clipboard.writeText(target.public_key); setNotice('公钥已复制') })}>复制公钥</button><button type="button" disabled={busy} onClick={() => { setEditing(target); setDraft({ name: target.name, host: target.host, port: target.port, user: target.user, host_fingerprint: target.host_fingerprint, commands: target.commands }) }}>编辑 {target.name}</button><button type="button" disabled={busy} onClick={() => void act(async () => { const result = await request<{ status: string; reason: string }>(`${url}/${target.id}/test`, { method: 'POST', csrfToken, onUnauthorized }); setNotice(result.status === 'pass' ? '连接通过，主机指纹匹配；未执行业务命令。' : `连接未验证：${result.reason}`) })}>测试连接</button><button type="button" disabled={busy} onClick={() => void act(async () => { await request(`${url}/${target.id}`, { method: 'DELETE', csrfToken, onUnauthorized, body: { revision: target.revision } }); if (editing?.id === target.id) { setEditing(null); setDraft(blank) } await load(); setNotice('目标与对应私钥已删除') })}>删除 {target.name}</button></div></article>)}
    <form onSubmit={save}><h3>{editing ? `编辑 ${editing.name}` : '注册新目标'}</h3><div className="wb-advanced-body">{(['name', 'host', 'user', 'host_fingerprint'] as const).map(key => <label key={key}>{{ name: '目标名称', host: '主机地址', user: '专用 SSH 用户', host_fingerprint: '主机 SHA256 指纹' }[key]}<input required disabled={busy} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: e.target.value })} /></label>)}<label>SSH 端口<input required disabled={busy} type="number" min={1} max={65535} value={draft.port} onChange={e => setDraft({ ...draft, port: Number(e.target.value) })} /></label>{Object.entries(labels).map(([key, label]) => <label key={key}>{label}<input disabled={busy} maxLength={4000} value={draft.commands[key] || ''} onChange={e => setDraft({ ...draft, commands: { ...draft.commands, [key]: e.target.value } })} /></label>)}</div><p>空白动作保持未配置。命令中不要填写凭据；脚本由目标服务器自行拉取产物，本平台不传输文件。</p><button disabled={busy} className="wb-button wb-button-primary">{busy ? '处理中…' : '保存目标'}</button>{editing && <button type="button" disabled={busy} onClick={() => { setEditing(null); setDraft(blank) }}>取消编辑</button>}</form>
    {notice && <p role="status">{notice}</p>}{error && <ErrorNotice message={error} />}</section>
}

export function ProjectTargets({ projectId, csrfToken, onUnauthorized, isAdmin }: Props & { projectId: string; isAdmin: boolean }) {
  const [catalog, setCatalog] = useState<Array<{ id: string; name: string }>>([])
  const [selected, setSelected] = useState<string[]>([])
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const bindingUrl = `/api/v2/projects/${encodeURIComponent(projectId)}/deploy-targets`
  useEffect(() => { const controller = new AbortController(); void (async () => { try { const bindings = await request<{ targets: string[]; revision: number; available: Array<{ id: string; name: string }> }>(bindingUrl, { onUnauthorized, signal: controller.signal }); const options = isAdmin ? (await request<{ targets: ServerTarget[] }>(url, { onUnauthorized, signal: controller.signal })).targets : bindings.available; if (!controller.signal.aborted) { setCatalog(options); setSelected(bindings.targets); setRevision(bindings.revision) } } catch (e) { if (!controller.signal.aborted) setError(errorText(e)) } })(); return () => controller.abort() }, [bindingUrl, isAdmin, onUnauthorized])
  return <section className="wb-card"><h2>项目服务器</h2><p>仅绑定的目标可用于此项目。成员可发起部署准备，连接设置由管理员维护。</p>{catalog.map(target => <label className="wb-checkbox" key={target.id}><input type="checkbox" disabled={!isAdmin || busy} checked={selected.includes(target.id)} onChange={e => { setSaved(false); setSelected(e.target.checked ? [...selected, target.id] : selected.filter(id => id !== target.id)) }} />{target.name}</label>)}{!catalog.length && <p>未连接服务器，仅完成准备。管理员可在运行配置中注册目标。</p>}{isAdmin && <button disabled={busy} onClick={() => { setBusy(true); setError(null); void request<{ revision: number }>(bindingUrl, { method: 'PUT', csrfToken, onUnauthorized, body: { revision, targets: selected } }).then(r => { setRevision(r.revision); setSaved(true) }).catch(e => setError(errorText(e))).finally(() => setBusy(false)) }}>保存项目绑定</button>}{saved && <p role="status">绑定已保存</p>}{error && <ErrorNotice message={error} />}</section>
}
