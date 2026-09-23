import { useCallback, useEffect, useRef, useState } from 'react'
import { useMaintenancePath } from './base-path'
import type { FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText } from '../workbench/ui'
import SyntheticBadge from './SyntheticBadge'
import type { PageProps } from '../workbench/ui'
import { maintenanceApi } from './api'
import type { Finding, RepoView } from './types'
import { REPO_STATE_LABEL } from './types'

const FINDING_LABEL: Record<Finding['status'], string> = {
  found: '已发现', verified: '已验证', missing: '缺失', failed: '失败',
}
const FINDING_TONE: Record<Finding['status'], string> = {
  found: 'wb-status-neutral', verified: 'wb-status-success', missing: 'wb-status-warning', failed: 'wb-status-danger',
}

function StateBadge({ state, label }: { state: RepoView['state']; label: string }) {
  const tone = state === 'ready' ? 'success' : state === 'failed' ? 'danger'
    : state === 'needs_input' ? 'warning' : state === 'analyzing' ? 'active' : 'neutral'
  return <span className={`wb-status wb-status-${tone}`}><i aria-hidden="true" />{label}</span>
}

// --- Register form ---

function RegisterForm({ csrfToken, onUnauthorized, onRegistered }: PageProps & { onRegistered: (repo: RepoView) => void }) {
  const [source, setSource] = useState('')
  const [name, setName] = useState('')
  const [branch, setBranch] = useState('')
  const [credentialRef, setCredentialRef] = useState('')
  const [synthetic, setSynthetic] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !source.trim() || !name.trim()) return
    setBusy(true); setError(null)
    try {
      const repo = await maintenanceApi.registerRepo(
        { source: source.trim(), name: name.trim(), branch: branch.trim() || undefined, credential_ref: credentialRef.trim() || undefined, synthetic },
        { csrfToken, onUnauthorized },
      )
      onRegistered(repo)
      setSource(''); setName(''); setBranch(''); setCredentialRef(''); setSynthetic(false)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="wb-card wb-create-card" aria-labelledby="repo-register-title">
      <div className="wb-card-head">
        <div>
          <span className="wb-eyebrow">登记代码库</span>
          <h2 id="repo-register-title">接入一个代码库</h2>
          <p>已登记过的同一仓库会复用既有项目，不会重复创建。分析在后台进行，不阻塞提交。</p>
        </div>
      </div>
      <form className="wb-form" onSubmit={submit}>
        <label>
          代码库地址或本地目录
          <input required value={source} onChange={e => setSource(e.target.value)} placeholder="git@github.com:org/repo.git 或 执行主机上的本地目录" />
          <small>本地路径指执行主机可访问的路径，不代表网页能直接读取用户电脑上的文件。</small>
        </label>
        <label>
          项目名称
          <input required value={name} onChange={e => setName(e.target.value)} placeholder="用于在列表中识别这个代码库" />
        </label>
        <details className="wb-advanced">
          <summary>分支与访问配置（可选）</summary>
          <div className="wb-advanced-body">
            <div className="wb-form-grid wb-form-grid-two">
              <label>
                分支
                <input value={branch} onChange={e => setBranch(e.target.value)} placeholder="留空则使用默认分支" />
              </label>
              <label>
                凭据引用名
                <input value={credentialRef} onChange={e => setCredentialRef(e.target.value)} placeholder="github" />
                <small>GitHub 仓库留空即使用平台 GitHub 凭据；只能填平台已配置的名称（目前为 github），不要粘贴密钥。</small>
              </label>
            </div>
            <label className="wb-checkbox">
              <input type="checkbox" checked={synthetic} onChange={e => setSynthetic(e.target.checked)} />
              这是合成/演示仓库（其需求、任务与回执都会标注为合成）
            </label>
          </div>
        </details>
        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button className="wb-button wb-button-primary" disabled={busy}>{busy ? '提交中…' : '登记代码库'}</button>
        </div>
      </form>
    </section>
  )
}

// --- List ---

function FailureNote({ access }: { access: NonNullable<RepoView['probe']>['access'] }) {
  return <div className="ms-repo-failure" role="note">
    <strong>{access.message}</strong>
    {access.next_step && <span>下一步：{access.next_step}</span>}
    {access.detail && <details><summary>技术详情（已脱敏）</summary><code>{access.detail}</code></details>}
  </div>
}

function RepoList(props: PageProps) {
  const mp = useMaintenancePath()
  const { onUnauthorized, csrfToken, user } = props
  const isAdmin = user?.role !== 'member'
  const [retrying, setRetrying] = useState<string | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [repos, setRepos] = useState<RepoView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)

  const load = useCallback(() => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    maintenanceApi.repos({ onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) { setRepos(result.repos); setError(null); setForbidden(false) } })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        if (cause instanceof WorkspaceApiError && cause.status === 403) { setForbidden(true); return }
        setError(errorText(cause))
      })
  }, [onUnauthorized])

  useEffect(() => { load(); return () => controllerRef.current?.abort() }, [load])

  const retry = async (projectId: string) => {
    setRetrying(projectId); setRetryError(null)
    try {
      const next = await maintenanceApi.probeRepo(projectId, { csrfToken, onUnauthorized })
      setRepos(current => (current ?? []).map(r => (r.project_id === projectId ? next : r)))
    } catch (cause) { setRetryError(errorText(cause)) } finally { setRetrying(null) }
  }

  // 有仓库在分析中时才轮询，避免空转请求。
  useEffect(() => {
    const busy = (repos ?? []).some(r => r.state === 'analyzing' || r.state === 'pending')
    if (!busy) return
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [repos, load])

  return (
    <div className="wb-page">
      <PageHeader title="维护代码库" description="登记后系统解析实际版本、识别构建与检查；「可开始维护」不代表构建、部署与业务检查通过。" />

      <RegisterForm {...props} onRegistered={repo => setRepos(current => {
        if (!current) return [repo]
        const exists = current.some(r => r.project_id === repo.project_id)
        return exists ? current.map(r => (r.project_id === repo.project_id ? repo : r)) : [repo, ...current]
      })} />

      {forbidden && <ErrorNotice message="没有权限查看代码库列表。" />}
      {error && <ErrorNotice message={error} />}
      {retryError && <ErrorNotice message={retryError} />}

      {!forbidden && !error && repos === null && (
        <div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div>
      )}

      {!forbidden && repos !== null && repos.length === 0 && (
        <div className="wb-card">
          <EmptyState title="还没有登记代码库" description="登记后即可在这里跟踪接入进度，并在「需求接入」里提交需求。" />
        </div>
      )}

      {repos !== null && repos.length > 0 && (
        <section className="wb-card" aria-label="代码库列表">
          {repos.map(repo => (
            <div className="ms-repo-row" key={repo.project_id}>
              <div>
                <Link className="wb-table-link" to={mp(`/repos/${encodeURIComponent(repo.project_id)}`)}>{repo.name}</Link> <SyntheticBadge synthetic={repo.synthetic} />
                <small>{repo.repository}{repo.probe?.branch ? `　分支 ${repo.probe.branch}` : ''}{repo.probe?.head_sha ? `　${repo.probe.head_sha.slice(0, 8)}` : ''}</small>
              </div>
              <div className="ms-repo-state">
                <StateBadge state={repo.state} label={repo.state_label || REPO_STATE_LABEL[repo.state]} />
                {repo.state === 'failed' && isAdmin && (
                  <button className="wb-button wb-button-secondary" disabled={retrying === repo.project_id}
                    onClick={() => void retry(repo.project_id)}>{retrying === repo.project_id ? '处理中…' : '重新接入'}</button>
                )}
              </div>
              {repo.state === 'failed' && repo.probe?.access && !repo.probe.access.ok && <FailureNote access={repo.probe.access} />}
            </div>
          ))}
        </section>
      )}
    </div>
  )
}

// --- Detail ---

function RepoDetail({ csrfToken, onUnauthorized, projectId }: PageProps & { projectId: string }) {
  const mp = useMaintenancePath()
  const navigate = useNavigate()
  const [repo, setRepo] = useState<RepoView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  const load = useCallback(() => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    maintenanceApi.repo(projectId, { onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) { setRepo(result); setError(null); setForbidden(false) } })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        if (cause instanceof WorkspaceApiError && cause.status === 403) { setForbidden(true); return }
        setError(errorText(cause))
      })
  }, [projectId, onUnauthorized])

  useEffect(() => { load(); return () => controllerRef.current?.abort() }, [load])

  useEffect(() => {
    if (!repo || (repo.state !== 'analyzing' && repo.state !== 'pending')) return
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [repo, load])

  const reprobe = async () => {
    setBusy(true); setActionError(null)
    try { setRepo(await maintenanceApi.probeRepo(projectId, { csrfToken, onUnauthorized })) }
    catch (cause) { setActionError(errorText(cause)) }
    finally { setBusy(false) }
  }

  const toggleSynthetic = async () => {
    if (!repo) return
    setBusy(true); setActionError(null)
    try { setRepo(await maintenanceApi.markSynthetic(projectId, !repo.synthetic, { csrfToken, onUnauthorized })) }
    catch (cause) { setActionError(errorText(cause)) }
    finally { setBusy(false) }
  }

  const adoptAll = async () => {
    if (!repo?.probe?.suggested_checks.length) return
    setBusy(true); setActionError(null)
    try { setRepo(await maintenanceApi.adoptChecks(projectId, repo.probe.suggested_checks.map(c => c.name), { csrfToken, onUnauthorized })) }
    catch (cause) { setActionError(errorText(cause)) }
    finally { setBusy(false) }
  }

  if (forbidden) return <div className="wb-page"><ErrorNotice message="没有权限查看这个代码库。" /></div>
  if (error) return <div className="wb-page"><ErrorNotice message={error} /></div>
  if (!repo) return <div className="wb-page"><div className="wb-card"><div className="wb-list-placeholder"><span /><span /><span /></div></div></div>

  const probe = repo.probe

  return (
    <div className="wb-page">
      <PageHeader
        title={repo.name}
        description={repo.repository}
        actions={<>
          <button className="wb-button wb-button-secondary" onClick={() => navigate(mp('/repos'))}>返回列表</button>
          <button className="wb-button wb-button-secondary" disabled={busy} onClick={() => void reprobe()}>{busy ? '处理中…' : repo.state === 'failed' ? '重新接入' : '重新分析'}</button>
          <button className="wb-button wb-button-secondary" disabled={busy} onClick={() => void toggleSynthetic()}
            title="只影响此后接收的需求；已创建的任务与回执保持当时的标注">
            {repo.synthetic ? '取消合成标记' : '标记为合成仓库'}</button>
        </>}
      />

      {actionError && <ErrorNotice message={actionError} />}

      <div className="ms-two-col">
        <div>
          <section className="wb-card" aria-label="接入状态">
            <div className="wb-card-head"><div><span className="wb-eyebrow">状态</span><h2><StateBadge state={repo.state} label={repo.state_label} /> <SyntheticBadge synthetic={repo.synthetic} /></h2></div></div>
            <div className="ms-note">「可开始维护」只表示工作区可用、基线可解析、至少一条检查已配置；不代表构建、部署与业务检查通过。</div>
            {repo.needs.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <span className="wb-eyebrow">待补充</span>
                <ul className="ms-side-list" style={{ marginTop: 8 }}>{repo.needs.map((n, i) => <li key={i}>{n}</li>)}</ul>
              </div>
            )}
          </section>

          {probe && (
            <section className="wb-card" aria-label="分析结果" style={{ marginTop: 18 }}>
              <div className="wb-card-head">
                <div><span className="wb-eyebrow">分析结果</span><h2>探测详情</h2></div>
              </div>
              <div className="wb-form-grid wb-form-grid-two" style={{ marginBottom: 16 }}>
                <div><span className="wb-eyebrow">基线 SHA</span><p>{probe.head_sha ? probe.head_sha.slice(0, 12) : '—'}</p></div>
                <div><span className="wb-eyebrow">分支</span><p>{probe.branch ?? '—'}</p></div>
                <div><span className="wb-eyebrow">访问</span><p><span className={`wb-status ${probe.access.ok ? 'wb-status-success' : 'wb-status-danger'}`}><i aria-hidden="true" />{probe.access.ok ? '可访问' : '不可访问'}</span>{probe.access.message && <small style={{ display: 'block', marginTop: 4 }}>{probe.access.message}</small>}{!probe.access.ok && probe.access.next_step && <small style={{ display: 'block', marginTop: 4 }}>下一步：{probe.access.next_step}</small>}</p></div>
                <div><span className="wb-eyebrow">分析时间</span><p>{probe.at ?? '—'}</p></div>
              </div>

              {probe.stack.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <span className="wb-eyebrow">技术栈（已发现，不等于已验证）</span>
                  <ul className="ms-stack-list" style={{ marginTop: 8 }}>
                    {probe.stack.map((s, i) => <li key={i}><strong>{s.name}</strong><span>{s.evidence}</span></li>)}
                  </ul>
                </div>
              )}

              {probe.findings.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <span className="wb-eyebrow">发现项</span>
                  <div data-testid="finding-list">
                    {probe.findings.map(f => (
                      <div className="ms-finding-row" key={f.id}>
                        <div><strong>{f.label}</strong>{f.message && <span className="ms-finding-msg">{f.message}</span>}</div>
                        <span className={`wb-status ${FINDING_TONE[f.status]}`} data-testid={`finding-${f.status}`}><i aria-hidden="true" />{FINDING_LABEL[f.status]}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {probe.suggested_checks.length > 0 && (
                <div>
                  <div className="wb-card-head" style={{ marginBottom: 8 }}>
                    <span className="wb-eyebrow">建议检查</span>
                    <button className="wb-button wb-button-primary" disabled={busy} onClick={() => void adoptAll()}>采纳建议检查</button>
                  </div>
                  <ul className="ms-check-list">
                    {probe.suggested_checks.map((c, i) => (
                      <li key={i}><strong>{c.name}</strong><div className="ms-check-argv">{c.argv.join(' ')}</div><span>{c.evidence}</span>
                        {c.available === false && <span className="ms-check-warn">执行主机上无法启动这条命令，采纳后检查会失败</span>}</li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          )}

          {repo.checks_configured.length > 0 && (
            <section className="wb-card" aria-label="已配置检查" style={{ marginTop: 18 }}>
              <div className="wb-card-head"><div><span className="wb-eyebrow">已配置</span><h2>检查命令</h2></div></div>
              <ul className="ms-side-list">{repo.checks_configured.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </section>
          )}

          <section className="wb-card" aria-label="项目记忆" style={{ marginTop: 18 }}>
            <div className="wb-card-head"><div><span className="wb-eyebrow">项目记忆</span><h2>记忆与代码索引</h2></div></div>
            <div className="wb-form-grid wb-form-grid-two">
              <div><span className="wb-eyebrow">条目</span><p>{repo.memory.entries ?? '—'}</p></div>
              <div><span className="wb-eyebrow">已确认</span><p>{repo.memory.confirmed ?? '—'}</p></div>
              <div className="wb-span-two"><span className="wb-eyebrow">代码索引</span><p>{repo.memory.code_index === 'on_demand' ? '执行时现场检索，未预建索引' : repo.memory.code_index}</p></div>
            </div>
          </section>

          {(repo.requirements?.length || repo.tasks?.length) ? (
            <section className="wb-card" aria-label="关联需求与任务" style={{ marginTop: 18 }}>
              <div className="wb-card-head"><div><span className="wb-eyebrow">关联</span><h2>需求与任务</h2></div></div>
              {repo.requirements && repo.requirements.length > 0 && (
                <div style={{ marginBottom: 14 }}>
                  <span className="wb-eyebrow">需求</span>
                  <ul className="ms-side-list" style={{ marginTop: 8 }}>
                    {repo.requirements.map(r => <li key={r.requirement_id}>{r.title || r.content.slice(0, 40)}</li>)}
                  </ul>
                </div>
              )}
              {repo.tasks && repo.tasks.length > 0 && (
                <div>
                  <span className="wb-eyebrow">任务</span>
                  <ul className="ms-side-list" style={{ marginTop: 8 }}>
                    {repo.tasks.map(t => <li key={t.task_id}><Link to={mp(`/${encodeURIComponent(t.task_id)}`)}>{t.title || t.task_id.slice(0, 8)}</Link>　{t.status}</li>)}
                  </ul>
                </div>
              )}
            </section>
          ) : null}
        </div>

        <aside>
          <section className="wb-card" aria-label="接入后系统负责什么">
            <div className="wb-card-head"><div><span className="wb-eyebrow">说明</span><h2>接入后系统负责什么</h2></div></div>
            <ul className="ms-side-list">
              <li>解析仓库的实际版本、识别可用的构建与检查命令</li>
              <li>接收需求后自动排入维护任务，按 SOP 步骤执行</li>
              <li>为每次交付记录检查结果、证据与产物</li>
              <li>维护项目记忆，供后续任务复用已确认的信息</li>
            </ul>
            <div className="ms-note" style={{ marginTop: 14 }}>不负责：构建部署、上线发布与业务验收；这些仍需人工确认。</div>
          </section>
          {repo.credential_ref && (
            <section className="wb-card" aria-label="访问配置" style={{ marginTop: 18 }}>
              <div className="wb-card-head"><div><span className="wb-eyebrow">访问</span><h2>凭据引用</h2></div></div>
              <p>{repo.credential_ref}</p>
            </section>
          )}
        </aside>
      </div>
    </div>
  )
}

export default function ReposPage(props: PageProps) {
  const { projectId } = useParams<{ projectId?: string }>()
  if (projectId) return <RepoDetail {...props} projectId={projectId} />
  return <RepoList {...props} />
}
