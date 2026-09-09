import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, formatDate, PageHeader, errorText, type PageProps } from './ui'
import './team.css'

function localBillingEnabled(): boolean { return false }

interface Quota { limit_tokens: number | null; used_tokens: number; reserved_tokens: number; unknown_calls: number; remaining_tokens: number | null }
interface TeamMember { id: string | number; username: string; role: 'admin' | 'member'; active: boolean; project_ids: string[]; quota: Quota }
interface TeamProject { id: string; name: string; member_ids: number[]; quota: Quota }
interface TeamCall { id: string; run_id: string | null; actor_id: string | number; project_id: string; provider?: string; model?: string; month: string; reserved_tokens: number; actual_tokens: number | null; status: 'reserved' | 'settled' | 'unknown'; created_at: string }
interface TeamData { month: string; timezone: string; tracking_started_at?: string | null; reservation_tokens: number; workspace: Quota; members: TeamMember[]; projects: TeamProject[]; calls: TeamCall[]; audit: Array<{ id?: string | number; action?: string; actor?: string; data?: unknown; at?: string }> }

const auditLabel: Record<string, string> = { 'member.created': '新增成员', 'member.updated': '更新成员', 'member.projects': '更新项目权限', 'quota.updated': '更新额度', 'reservation.updated': '更新调度设置', 'usage.reconciled': '核对调用', 'member.password_changed': '修改密码' }
function tokens(value: number | null | undefined): string { return value === null || value === undefined ? '不限额' : new Intl.NumberFormat('zh-CN').format(value) }
function limitLabel(quota: Quota): string { return quota.limit_tokens === null ? '不限额' : `${tokens(quota.remaining_tokens)} 剩余` }
function QuotaSummary({ quota, label }: { quota: Quota; label: string }) {
  return <div className="tm-quota-summary"><div><span>{label}</span><strong>{tokens(quota.used_tokens)}</strong><small>已使用 tokens</small></div><div><span>正在占用</span><strong>{tokens(quota.reserved_tokens)}</strong><small>已预留 tokens</small></div><div><span>可用额度</span><strong>{limitLabel(quota)}</strong><small>{quota.limit_tokens === null ? '未设置上限' : `上限 ${tokens(quota.limit_tokens)}`}</small></div><div className={quota.unknown_calls > 0 ? 'is-warning' : ''}><span>待核对</span><strong>{tokens(quota.unknown_calls)}</strong><small>笔用量待核对</small></div></div>
}

function TeamPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const isAdmin = user?.role !== 'member'
  const [data, setData] = useState<TeamData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [newMemberOpen, setNewMemberOpen] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState<'member' | 'admin'>('member')
  const [workspaceLimit, setWorkspaceLimit] = useState('')
  const [reservationTokens, setReservationTokens] = useState('20000')
  const [passwords, setPasswords] = useState({ current: '', next: '' })
  const controllerRef = useRef<AbortController | null>(null)

  const load = () => {
    controllerRef.current?.abort(); const controller = new AbortController(); controllerRef.current = controller; setLoading(true); setError(null)
    void request<TeamData>('/api/v3/team', { onUnauthorized, signal: controller.signal }).then((value) => { if (!controller.signal.aborted) { setData(value); setWorkspaceLimit(value.workspace.limit_tokens === null ? '' : String(value.workspace.limit_tokens)); setReservationTokens(String(value.reservation_tokens)) } }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)) }).finally(() => { if (controllerRef.current === controller && !controller.signal.aborted) setLoading(false) })
    return controller
  }
  useEffect(() => { const controller = load(); return () => controller.abort() }, [onUnauthorized])

  const unknownCalls = (data?.calls ?? []).filter((call) => call.status === 'unknown')
  const calls = data?.calls ?? []
  const displayQuota = !isAdmin && data?.members[0] ? data.members[0].quota : data?.workspace
  const save = async (key: string, action: () => Promise<void>) => { setBusy(key); setError(null); setNotice(null); try { await action(); setNotice('团队设置已保存。'); load() } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(null) } }
  const createMember = async (event: FormEvent) => { event.preventDefault(); if (!newUsername.trim() || newPassword.length < 12) { setError('新密码至少需要 12 个字符。'); return }; await save('new-member', async () => { await request('/api/v3/team/members', { method: 'POST', csrfToken, onUnauthorized, body: { username: newUsername.trim(), password: newPassword, role: newRole } }); setNewUsername(''); setNewPassword(''); setNewRole('member'); setNewMemberOpen(false) }) }
  const updateMember = async (member: TeamMember, role: 'admin' | 'member', active: boolean, password: string) => { if (password && password.length < 12) { setError('新密码至少需要 12 个字符。'); return } return save(`member-${member.id}`, async () => { await request(`/api/v3/team/members/${encodeURIComponent(String(member.id))}`, { method: 'PUT', csrfToken, onUnauthorized, body: { role, active, ...(password ? { password } : {}) } }) }) }
  const updateProjects = async (member: TeamMember, projectIds: string[]) => save(`projects-${member.id}`, async () => { await request(`/api/v3/team/members/${encodeURIComponent(String(member.id))}/projects`, { method: 'PUT', csrfToken, onUnauthorized, body: { project_ids: projectIds } }) })
  const updateQuota = async (scope: 'workspace' | 'project' | 'member', scopeId: string, value: string) => save(`${scope}-${scopeId}`, async () => { const limit_tokens = value.trim() === '' ? null : Number(value); if (limit_tokens !== null && (!Number.isInteger(limit_tokens) || limit_tokens < 0)) throw new Error('额度需要是非负整数，留空表示不限额。'); await request('/api/v3/team/quotas', { method: 'PUT', csrfToken, onUnauthorized, body: { scope, scope_id: scopeId, limit_tokens } }) })
  const updateSettings = async (event: FormEvent) => { event.preventDefault(); await save('settings', async () => { const value = Number(reservationTokens); if (!Number.isInteger(value) || value < 1 || value > 1000000) throw new Error('单次调用预留需要是 1 至 1,000,000 的整数。'); await request('/api/v3/team/settings', { method: 'PUT', csrfToken, onUnauthorized, body: { reservation_tokens: value } }) }) }
  const changePassword = async (event: FormEvent) => { event.preventDefault(); await save('password', async () => { await request('/api/auth/password', { method: 'POST', csrfToken, onUnauthorized, body: { current_password: passwords.current, password: passwords.next } }); setPasswords({ current: '', next: '' }); onUnauthorized() }) }
  const reconcile = async (call: TeamCall, actual: string, reason: string) => save(`call-${call.id}`, async () => { if (!actual.trim() || reason.trim().length < 3) throw new Error('请填写非负实际 tokens，核对原因至少 3 个字符。'); const actual_tokens = Number(actual); if (!Number.isInteger(actual_tokens) || actual_tokens < 0) throw new Error('实际 tokens 需要是非负整数。'); await request(`/api/v3/team/calls/${encodeURIComponent(call.id)}/reconcile`, { method: 'POST', csrfToken, onUnauthorized, body: { actual_tokens, reason: reason.trim() } }) })

  return <div className="tm-page">
    <PageHeader title="团队" description="管理成员与项目权限。token 额度由中转站管理。" actions={<button className="tm-button tm-button-secondary" onClick={() => void load()} disabled={loading}>{loading ? '更新中…' : '刷新'}</button>} />
    {error && <ErrorNotice message={error} />}{notice && <div className="tm-notice" role="status">{notice}</div>}
    {loading && !data && <div className="tm-loading" role="status"><span /><span /><span /></div>}
    {data && <>
      {localBillingEnabled() && <div className="tm-context"><strong>{data.month} · {data.timezone}</strong><span>计量从 {data.tracking_started_at ? formatDate(data.tracking_started_at) : '治理启用后'} 开始</span></div>}
      {localBillingEnabled() && <details className="tm-method-note"><summary>额度如何计算</summary><p>成员和项目共享运行记录；额度按每月 UTC 重置，统计模型输入与输出 tokens（含缓存输入）。额度是调度门槛，不是美元金额或供应商订阅 credits；单次执行可能超过预留值，待核对占用会保留到管理员核对。</p></details>}
      {localBillingEnabled() && displayQuota && <QuotaSummary quota={displayQuota} label={isAdmin ? '工作区本月用量' : '我的本月用量'} />}
      <section className="tm-card"><div className="tm-card-head"><div><span className="tm-kicker">小组成员</span><h2>成员与项目权限</h2><p>成员能看到小组共享记录，发起工作和配置修改仍按角色与项目分配限制。</p></div>{isAdmin && <button className="tm-button tm-button-primary" onClick={() => setNewMemberOpen((value) => !value)}>{newMemberOpen ? '关闭新增' : '新增成员'}</button>}</div>{newMemberOpen && isAdmin && <form className="tm-new-member" onSubmit={createMember}><label>用户名<input required value={newUsername} onChange={(event) => setNewUsername(event.target.value)} /></label><label>初始密码<input required minLength={12} type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label><label>角色<select value={newRole} onChange={(event) => setNewRole(event.target.value as 'member' | 'admin')}><option value="member">成员</option><option value="admin">管理员</option></select></label><button className="tm-button tm-button-primary" disabled={busy === 'new-member'}>{busy === 'new-member' ? '创建中…' : '创建成员'}</button></form>}{data.members.map((member) => <MemberRow key={member.id} member={member} projects={data.projects} isAdmin={isAdmin} busy={busy} onUpdate={updateMember} onProjects={updateProjects} onQuota={updateQuota} />)}</section>
      {localBillingEnabled() && <section className="tm-card"><div className="tm-card-head"><div><span className="tm-kicker">项目额度</span><h2>项目用量与上限</h2><p>项目额度是调度门槛，项目记录仍由小组共享。</p></div></div><div className="tm-project-list">{data.projects.map((project) => <ProjectQuotaRow key={project.id} project={project} isAdmin={isAdmin} busy={busy} onQuota={updateQuota} />)}</div></section>}
      {localBillingEnabled() && <section className="tm-card"><div className="tm-card-head"><div><span className="tm-kicker">调用记录</span><h2>调用与待核对占用</h2><p>未知调用保留在额度中；这里同时展示已预留和已结算记录，便于追溯。</p></div><span className="tm-count">{unknownCalls.length} 笔用量待核对</span></div>{calls.length === 0 ? <p className="tm-empty-line">当前没有调用记录。</p> : <div className="tm-call-list">{calls.slice(0, 100).map((call) => <CallRow key={call.id} call={call} isAdmin={isAdmin} busy={busy} onReconcile={reconcile} />)}</div>}</section>}
      {localBillingEnabled() && <details className="tm-card tm-control-card"><summary className="tm-card-head"><div><span className="tm-kicker">额度门槛</span><h2>工作区设置</h2><p>管理员调整单次调用预留和工作区月额度。</p></div><span className="tm-account-summary">展开设置</span></summary>{isAdmin ? <div className="tm-admin-grid"><form onSubmit={updateSettings}><label>单次调用预留<input type="number" min="1" max="1000000" value={reservationTokens} onChange={(event) => setReservationTokens(event.target.value)} /><small>1 至 1,000,000 tokens</small></label><button className="tm-button tm-button-primary" disabled={busy === 'settings'}>{busy === 'settings' ? '保存中…' : '保存调度设置'}</button></form><div><label>工作区月额度<input value={workspaceLimit} onChange={(event) => setWorkspaceLimit(event.target.value)} placeholder="留空表示不限额" inputMode="numeric" /></label><button className="tm-button tm-button-secondary" disabled={Boolean(busy)} onClick={() => void updateQuota('workspace', 'all', workspaceLimit)}>保存工作区额度</button></div></div> : <p className="tm-readonly">额度及成员权限由管理员维护。</p>}</details>}
      <details className="tm-card tm-account"><summary className="tm-card-head"><div><span className="tm-kicker">账号</span><h2>修改密码</h2><p>修改成功后需要重新登录。</p></div><span className="tm-account-summary">展开</span></summary><form className="tm-password-form" onSubmit={changePassword}><label>当前密码<input required type="password" value={passwords.current} onChange={(event) => setPasswords({ ...passwords, current: event.target.value })} /></label><label>新密码<input required minLength={12} type="password" value={passwords.next} onChange={(event) => setPasswords({ ...passwords, next: event.target.value })} /></label><button className="tm-button tm-button-primary" disabled={busy === 'password'}>{busy === 'password' ? '保存中…' : '修改密码'}</button></form></details>
      {isAdmin && data.audit.length > 0 && <section className="tm-card tm-audit"><div className="tm-card-head"><div><span className="tm-kicker">权限记录</span><h2>最近调整</h2></div></div>{data.audit.slice(0, 8).map((item, index) => <div className="tm-audit-row" key={String(item.id ?? index)}><span>{auditLabel[item.action ?? ''] ?? '权限变化'}</span><small>{item.actor ?? '管理员'} · {formatDate(item.at)}</small></div>)}</section>}
    </>}
  </div>
}

function QuotaMini({ quota }: { quota: Quota }) { return <span className="tm-quota-mini">已用 {tokens(quota.used_tokens)} / 上限 {quota.limit_tokens === null ? '不限额' : tokens(quota.limit_tokens)}</span> }

function ProjectQuotaRow({ project, isAdmin, busy, onQuota }: { project: TeamProject; isAdmin: boolean; busy: string | null; onQuota: (scope: 'project', id: string, value: string) => Promise<void> }) {
  const [limit, setLimit] = useState(project.quota.limit_tokens === null ? '' : String(project.quota.limit_tokens))
  useEffect(() => setLimit(project.quota.limit_tokens === null ? '' : String(project.quota.limit_tokens)), [project.quota.limit_tokens])
  return <div className="tm-project-row"><div><strong>{project.name}</strong><small>{project.member_ids.length} 位已分配成员 · {limitLabel(project.quota)}</small></div><QuotaMini quota={project.quota} />{isAdmin && <div className="tm-project-limit"><label><span className="tm-visually-hidden">{project.name} 月额度</span><input value={limit} onChange={(event) => setLimit(event.target.value)} placeholder="不限额" inputMode="numeric" /></label><button className="tm-button tm-button-secondary" disabled={busy === `project-${project.id}`} onClick={() => void onQuota('project', project.id, limit)}>保存额度</button></div>}</div>
}

function MemberRow({ member, projects, isAdmin, busy, onUpdate, onProjects, onQuota }: { member: TeamMember; projects: TeamProject[]; isAdmin: boolean; busy: string | null; onUpdate: (member: TeamMember, role: 'admin' | 'member', active: boolean, password: string) => Promise<void>; onProjects: (member: TeamMember, ids: string[]) => Promise<void>; onQuota: (scope: 'member', id: string, value: string) => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [role, setRole] = useState(member.role)
  const [active, setActive] = useState(member.active)
  const [password, setPassword] = useState('')
  const [selectedProjects, setSelectedProjects] = useState(member.project_ids)
  const [limit, setLimit] = useState(member.quota.limit_tokens === null ? '' : String(member.quota.limit_tokens))
  useEffect(() => { setRole(member.role); setActive(member.active); setSelectedProjects(member.project_ids); setLimit(member.quota.limit_tokens === null ? '' : String(member.quota.limit_tokens)) }, [member])
  return <article className={`tm-member-row ${!member.active ? 'is-disabled' : ''}`}>
    <div className="tm-member-identity"><span className="tm-avatar" aria-hidden="true">{member.username.slice(0, 1).toUpperCase()}</span><div><strong>{member.username}</strong><small>{member.role === 'admin' ? '管理员' : '成员'} · {member.active ? '可登录' : '已停用'} · {member.project_ids.length} 个项目</small></div></div>
    {localBillingEnabled() && <QuotaMini quota={member.quota} />}
    {isAdmin ? <button className="tm-button tm-button-secondary tm-manage-member" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? '收起管理' : '管理成员'}</button> : <span className="tm-member-readonly">{member.role === 'admin' ? '管理员' : '成员'} · 项目权限由管理员维护</span>}
    {isAdmin && open && <div className="tm-member-controls">
      <label>角色<select value={role} onChange={(event) => setRole(event.target.value as 'admin' | 'member')}><option value="member">成员</option><option value="admin">管理员</option></select></label>
      <label className="tm-check"><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />启用</label>
      <label>重置密码<input type="password" minLength={12} placeholder="留空不改（至少12位）" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      <button className="tm-button tm-button-secondary" disabled={busy === `member-${member.id}`} onClick={() => void onUpdate(member, role, active, password)}>保存成员</button>
      <fieldset className="tm-project-checks"><legend>项目权限</legend>{projects.length === 0 ? <small>还没有可分配的项目。</small> : projects.map((project) => <label key={project.id} className="tm-check"><input type="checkbox" checked={selectedProjects.includes(project.id)} onChange={(event) => setSelectedProjects((current) => event.target.checked ? [...new Set([...current, project.id])] : current.filter((id) => id !== project.id))} />{project.name}</label>)}</fieldset>
      <button className="tm-button tm-button-secondary" disabled={busy === `projects-${member.id}`} onClick={() => void onProjects(member, selectedProjects)}>保存项目权限</button>
      {localBillingEnabled() && <><label>月额度<input value={limit} placeholder="不限额" inputMode="numeric" onChange={(event) => setLimit(event.target.value)} /></label>
      <button className="tm-button tm-button-secondary" disabled={busy === `member-${member.id}`} onClick={() => void onQuota('member', String(member.id), limit)}>保存成员额度</button></>}
    </div>}
  </article>
}

function CallRow({ call, isAdmin, busy, onReconcile }: { call: TeamCall; isAdmin: boolean; busy: string | null; onReconcile: (call: TeamCall, actual: string, reason: string) => Promise<void> }) {
  const [actual, setActual] = useState(''); const [reason, setReason] = useState('')
  const statusLabel = call.status === 'unknown' ? '待核对' : call.status === 'reserved' ? '已预留' : '已结算'
  return <div className={`tm-call-row tm-call-${call.status}`}><div><strong>{call.provider || '未记录供应商'} · {call.model || '未记录型号'} <span className="tm-call-status">{statusLabel}</span></strong><small>{call.run_id ? <Link to={`/runs/${encodeURIComponent(String(call.run_id))}`}>运行 #{call.run_id}</Link> : '运行探针'} · {formatDate(call.created_at)} · 预留 {tokens(call.reserved_tokens)} tokens{call.actual_tokens !== null ? ` · 实际 ${tokens(call.actual_tokens)} tokens` : ''}</small></div>{isAdmin && call.status === 'unknown' ? <div className="tm-reconcile"><input aria-label="实际 tokens" type="number" min="0" value={actual} onChange={(event) => setActual(event.target.value)} placeholder="实际 tokens" /><input aria-label="核对原因" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="核对原因（至少3字）" /><button className="tm-button tm-button-secondary" disabled={busy === `call-${call.id}` || !actual.trim() || reason.trim().length < 3} onClick={() => void onReconcile(call, actual, reason)}>核对</button></div> : call.status === 'unknown' ? <span className="tm-unknown">待管理员核对</span> : <span className="tm-settled">{statusLabel}</span>}</div>
}

export default TeamPage
