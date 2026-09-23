// 组织与授权（仅管理员）：组织树增删改移、项目归属绑定/解绑、成员管理查看范围授予/撤销。
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText, formatDate, type PageProps } from '../workbench/ui'
import { managementApi } from './api'
import { KIND_LABEL, type OrgTree, type OrgUnit, type UnitKind } from './types'
import './management.css'

function projectName(tree: OrgTree, id: string): string {
  return tree.projects.find((p) => p.id === id)?.name ?? id
}

function UnitRow({ unit, tree, busy, onRename, onMove, onDelete, onUnbind }: {
  unit: OrgUnit
  tree: OrgTree
  busy: string | null
  onRename: (unit: OrgUnit, name: string) => Promise<void>
  onMove: (unit: OrgUnit, parentId: string) => Promise<void>
  onDelete: (unit: OrgUnit) => Promise<void>
  onUnbind: (projectId: string) => Promise<void>
}) {
  const [name, setName] = useState(unit.name)
  const [parentId, setParentId] = useState(unit.parent_id ?? '')
  useEffect(() => { setName(unit.name); setParentId(unit.parent_id ?? '') }, [unit.name, unit.parent_id])
  const otherUnits = tree.units.filter((u) => u.id !== unit.id)
  return <div className="mg-tree-node">
    <div className="mg-tree-row">
      <div><strong>{unit.path}</strong> <small>{unit.kind_label}</small></div>
      <button type="button" className="wb-button wb-button-secondary" disabled={busy === `del-${unit.id}`}
        onClick={() => void onDelete(unit)}>删除</button>
    </div>
    <div className="mg-inline-form">
      <input aria-label={`${unit.name} 新名称`} value={name} onChange={(e) => setName(e.target.value)} />
      <button type="button" className="wb-button wb-button-secondary" disabled={busy === `rename-${unit.id}` || !name.trim()}
        onClick={() => void onRename(unit, name.trim())}>改名</button>
      {unit.parent_id !== null && <>
        <select aria-label={`${unit.name} 新上级`} value={parentId} onChange={(e) => setParentId(e.target.value)}>
          {otherUnits.map((u) => <option key={u.id} value={u.id}>{u.path}</option>)}
        </select>
        <button type="button" className="wb-button wb-button-secondary" disabled={busy === `move-${unit.id}` || !parentId}
          onClick={() => void onMove(unit, parentId)}>移动上级</button>
      </>}
    </div>
    {unit.project_ids.length > 0 && <ul className="mg-unassigned">
      {unit.project_ids.map((pid) => (
        <li key={pid}>{projectName(tree, pid)}
          <button type="button" className="wb-button wb-button-secondary" style={{ marginLeft: 8 }}
            disabled={busy === `unbind-${pid}`} onClick={() => void onUnbind(pid)}>解绑</button>
        </li>
      ))}
    </ul>}
  </div>
}

export default function OrgPage({ csrfToken, onUnauthorized, user }: PageProps) {
  const isAdmin = user?.role !== 'member'
  const [tree, setTree] = useState<OrgTree | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  const [newName, setNewName] = useState('')
  const [newKind, setNewKind] = useState<UnitKind>('department')
  const [newParent, setNewParent] = useState('')
  const [bindProject, setBindProject] = useState('')
  const [bindUnit, setBindUnit] = useState('')
  const [grantUser, setGrantUser] = useState('')
  const [grantUnit, setGrantUnit] = useState('')

  const load = () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    managementApi.orgTree({ onUnauthorized, signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setTree(value) })
      .catch((cause) => { if (!controller.signal.aborted && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return controller
  }
  useEffect(() => { if (!isAdmin) return; const c = load(); return () => c.abort() }, [isAdmin, onUnauthorized])

  if (!isAdmin) return <div className="mg-page"><PageHeader title="组织与授权" /><ErrorNotice message="此页面需要管理员权限" /></div>

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key); setError(null)
    try { await action(); load() } catch (cause) { if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(errorText(cause)) } finally { setBusy(null) }
  }

  const createUnit = async (event: FormEvent) => {
    event.preventDefault()
    if (!newName.trim()) { setError('组织名称不能为空。'); return }
    await run('create-unit', async () => {
      await managementApi.createUnit({ name: newName.trim(), kind: newKind, parent_id: newParent || null }, { csrfToken, onUnauthorized })
      setNewName(''); setNewParent('')
    })
  }
  const renameUnit = (unit: OrgUnit, name: string) => run(`rename-${unit.id}`, () => managementApi.updateUnit(unit.id, { name }, { csrfToken, onUnauthorized }).then(() => undefined))
  const moveUnit = (unit: OrgUnit, parentId: string) => run(`move-${unit.id}`, () => managementApi.updateUnit(unit.id, { parent_id: parentId }, { csrfToken, onUnauthorized }).then(() => undefined))
  const deleteUnit = (unit: OrgUnit) => run(`del-${unit.id}`, () => managementApi.deleteUnit(unit.id, { csrfToken, onUnauthorized }).then(() => undefined))
  const bind = async (event: FormEvent) => {
    event.preventDefault()
    if (!bindProject || !bindUnit) return
    await run(`bind-${bindProject}`, async () => { await managementApi.bindProject(bindProject, bindUnit, { csrfToken, onUnauthorized }); setBindProject(''); setBindUnit('') })
  }
  const unbind = (projectId: string) => run(`unbind-${projectId}`, () => managementApi.unbindProject(projectId, { csrfToken, onUnauthorized }).then(() => undefined))
  const grant = async (event: FormEvent) => {
    event.preventDefault()
    if (!grantUser || !grantUnit) return
    await run(`grant-${grantUser}-${grantUnit}`, async () => { await managementApi.grantScope(Number(grantUser), grantUnit, { csrfToken, onUnauthorized }); setGrantUser(''); setGrantUnit('') })
  }
  const revoke = (userId: number, unitId: string) => run(`revoke-${userId}-${unitId}`, () => managementApi.revokeScope(userId, unitId, { csrfToken, onUnauthorized }).then(() => undefined))

  const hasTop = Boolean(tree?.units.some((u) => u.parent_id === null))
  const eligibleMembers = (tree?.users ?? []).filter((u) => u.role === 'member' && u.active)

  return <div className="mg-page">
    <div className="mg-topbar"><Link to="/management" className="mg-back-link">← 返回管理首页</Link></div>
    <PageHeader title="组织与授权" description="维护组织树、项目归属，以及成员的管理查看范围。所有写操作都会在下一次请求生效。" />
    {error && <ErrorNotice message={error} />}
    {loading && !tree && <div role="status">加载中…</div>}
    {tree && <div className="mg-org-columns">
      <section className="wb-card">
        <div className="wb-card-head"><div><span className="wb-eyebrow">组织树</span><h2>组织节点</h2></div></div>
        <form className="mg-inline-form" onSubmit={createUnit}>
          <input aria-label="新组织名称" placeholder="名称" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <select aria-label="组织类型" value={newKind} onChange={(e) => setNewKind(e.target.value as UnitKind)}>
            {(['company', 'department', 'group'] as UnitKind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
          {hasTop && <select aria-label="上级组织" value={newParent} onChange={(e) => setNewParent(e.target.value)}>
            <option value="">选择上级…</option>
            {tree.units.map((u) => <option key={u.id} value={u.id}>{u.path}</option>)}
          </select>}
          <button type="button" className="wb-button wb-button-primary" disabled={busy === 'create-unit'} onClick={createUnit}>新建组织</button>
        </form>
        {tree.units.length === 0 ? <p className="mg-hint">还没有组织节点。</p> : tree.units.map((unit) => (
          <UnitRow key={unit.id} unit={unit} tree={tree} busy={busy} onRename={renameUnit} onMove={moveUnit} onDelete={deleteUnit} onUnbind={unbind} />
        ))}
      </section>

      <div>
        <section className="wb-card">
          <div className="wb-card-head"><div><span className="wb-eyebrow">项目归属</span><h2>未归属项目</h2></div></div>
          {tree.unassigned_project_ids.length === 0 ? <p className="mg-hint">所有项目都已归属组织。</p> : <>
            <ul className="mg-unassigned">{tree.unassigned_project_ids.map((pid) => <li key={pid}>{projectName(tree, pid)}</li>)}</ul>
            <form className="mg-inline-form" onSubmit={bind}>
              <select aria-label="选择项目" value={bindProject} onChange={(e) => setBindProject(e.target.value)}>
                <option value="">选择项目…</option>
                {tree.unassigned_project_ids.map((pid) => <option key={pid} value={pid}>{projectName(tree, pid)}</option>)}
              </select>
              <select aria-label="绑定到组织" value={bindUnit} onChange={(e) => setBindUnit(e.target.value)}>
                <option value="">绑定到…</option>
                {tree.units.map((u) => <option key={u.id} value={u.id}>{u.path}</option>)}
              </select>
              <button type="submit" className="wb-button wb-button-primary" disabled={!bindProject || !bindUnit || Boolean(busy)}>绑定</button>
            </form>
          </>}
        </section>

        <section className="wb-card">
          <div className="wb-card-head"><div><span className="wb-eyebrow">授权</span><h2>成员管理查看范围</h2></div></div>
          <form className="mg-inline-form" onSubmit={grant}>
            <select aria-label="选择成员" value={grantUser} onChange={(e) => setGrantUser(e.target.value)}>
              <option value="">选择成员…</option>
              {eligibleMembers.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
            </select>
            <select aria-label="授予组织" value={grantUnit} onChange={(e) => setGrantUnit(e.target.value)}>
              <option value="">授予范围…</option>
              {tree.units.map((u) => <option key={u.id} value={u.id}>{u.path}</option>)}
            </select>
            <button type="submit" className="wb-button wb-button-primary" disabled={!grantUser || !grantUnit || Boolean(busy)}>授予</button>
          </form>
          {tree.scopes.length === 0 ? <p className="mg-hint">还没有授权记录。</p> : tree.scopes.map((scope) => (
            <div className="mg-tree-row" key={`${scope.user_id}-${scope.unit_id}`}>
              <div><strong>{scope.username}</strong> <small>{scope.path} · 授予人 {scope.granted_by} · {formatDate(scope.granted_at)}</small></div>
              <button type="button" className="wb-button wb-button-secondary" disabled={busy === `revoke-${scope.user_id}-${scope.unit_id}`}
                onClick={() => void revoke(scope.user_id, scope.unit_id)}>撤销</button>
            </div>
          ))}
        </section>
      </div>
    </div>}
  </div>
}
