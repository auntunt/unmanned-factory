import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import Icon from '../workbench/Icon'
import { request } from '../workspace/api'
import type { WorkbenchProps } from '../workbench/ui'
import { PRIMARY_NAV, SETTINGS_NAV, GROUP_TABS, resolveRoute, type NavItem } from './nav-config'
import { WorkTitleContext } from './title-context'
import './app-shell.css'

const COLLAPSE_KEY = 'webuddy:nav:collapsed'

/** The single application shell for every logged-in route. It never remounts on
 *  navigation: the left nav, top bar and account stay fixed; only the selected
 *  item, breadcrumb, title and the page in <Outlet/> change. */
export default function AppShell({ user, onLogout }: WorkbenchProps) {
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem(COLLAPSE_KEY) === '1' } catch { return false } })
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [workTitle, setWorkTitle] = useState<string | null>(null)
  const [env, setEnv] = useState<{ mode?: string; label?: string } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const hamburgerRef = useRef<HTMLButtonElement>(null)

  const resolved = useMemo(() => resolveRoute(location.pathname), [location.pathname])
  const title = resolved.usesWorkTitle && workTitle ? workTitle : resolved.title

  useEffect(() => { try { localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0') } catch { /* optional */ } }, [collapsed])
  useEffect(() => { setDrawerOpen(false); setMenuOpen(false); setWorkTitle(null) }, [location.pathname])
  useEffect(() => { const c = new AbortController(); request<{ mode?: string; label?: string }>('/api/v3/environment', { onUnauthorized: onLogout, signal: c.signal }).then(v => { if (!c.signal.aborted) setEnv(v) }).catch(() => undefined); return () => c.abort() }, [onLogout])

  // account popover: outside click + Esc
  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDoc); document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [menuOpen])

  // mobile drawer: scroll lock, focus in, Esc/scrim close, focus trap, focus back to trigger
  useEffect(() => {
    if (!drawerOpen) return
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const first = drawerRef.current?.querySelector<HTMLElement>('a,button')
    first?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setDrawerOpen(false); return }
      if (e.key === 'Tab' && drawerRef.current) {
        const nodes = Array.from(drawerRef.current.querySelectorAll<HTMLElement>('a,button')).filter(n => !n.hasAttribute('disabled'))
        if (nodes.length === 0) return
        const firstN = nodes[0], lastN = nodes[nodes.length - 1]
        if (e.shiftKey && document.activeElement === firstN) { e.preventDefault(); lastN.focus() }
        else if (!e.shiftKey && document.activeElement === lastN) { e.preventDefault(); firstN.focus() }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prevOverflow; hamburgerRef.current?.focus() }
  }, [drawerOpen])

  const isAdmin = user.role !== 'member'
  const initial = Array.from(user.username || 'u')[0]?.toUpperCase() ?? 'U'
  const navItems: NavItem[] = PRIMARY_NAV

  const renderNav = (onNavigate?: () => void) => (
    <nav className="as-nav" aria-label="主导航">
      {navItems.map(item => (
        <NavLink key={item.key} to={item.to} end={item.to === '/'} onClick={onNavigate}
          className={() => `as-nav-item ${resolved.activeKey === item.key ? 'is-active' : ''}`}
          aria-current={resolved.activeKey === item.key ? 'page' : undefined} aria-label={item.label} title={collapsed ? item.label : undefined}>
          <span className="as-nav-icon"><Icon name={item.icon} /></span><span className="as-nav-text">{item.label}</span>
        </NavLink>
      ))}
    </nav>
  )

  const footer = (onNavigate?: () => void) => (
    <div className="as-side-footer">
      <NavLink to={SETTINGS_NAV.to} end onClick={onNavigate} className={() => `as-nav-item ${resolved.activeKey === 'settings' ? 'is-active' : ''}`}
        aria-current={resolved.activeKey === 'settings' ? 'page' : undefined} aria-label={SETTINGS_NAV.label} title={collapsed ? SETTINGS_NAV.label : undefined}>
        <span className="as-nav-icon"><Icon name={SETTINGS_NAV.icon} /></span><span className="as-nav-text">{SETTINGS_NAV.label}</span>
      </NavLink>
      <div className="as-account-wrap" ref={menuRef}>
        <button className="as-account" aria-haspopup="menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(o => !o)} title={collapsed ? user.username : undefined}>
          <span className="as-avatar">{initial}</span>
          <span className="as-account-name"><strong>{user.username}</strong><small>{isAdmin ? '工作区管理员' : '成员'}</small></span>
          <span className="as-account-caret"><Icon name="triangle" width={14} height={14} /></span>
        </button>
        {menuOpen && <div className="as-account-menu" role="menu">
          <div className="as-account-info"><strong>{user.username}</strong><small>{isAdmin ? '工作区管理员' : '成员'}</small></div>
          <button role="menuitem" onClick={onLogout}><Icon name="logout" width={16} height={16} /> 退出登录</button>
        </div>}
      </div>
    </div>
  )

  const brand = (
    <div className="as-brand">
      <Link to="/" className="as-brand-mark" aria-label="webuddy 首页">webuddy</Link>
      <button className="as-collapse" onClick={() => setCollapsed(c => !c)} aria-label={collapsed ? '展开导航' : '收起导航'} title={collapsed ? '展开导航' : '收起导航'}>
        <Icon name={collapsed ? 'arrow' : 'back'} width={16} height={16} />
      </button>
    </div>
  )

  return (
    <div className={`as-app ${collapsed ? 'is-collapsed' : ''}`}>
      <a className="as-skip" href="#as-content">跳到主要内容</a>
      <aside className="as-side" aria-label="工作区导航">
        {brand}
        {renderNav()}
        {footer()}
      </aside>

      <div className="as-body">
        <header className="as-topbar">
          <button className="as-hamburger" ref={hamburgerRef} aria-label="打开导航" aria-expanded={drawerOpen} onClick={() => setDrawerOpen(true)}><Icon name="menu" width={20} height={20} /></button>
          <nav className="as-crumbs" aria-label="面包屑">
            {resolved.breadcrumb.map((c, i) => <span className="as-crumb" key={`${c.label}-${i}`}>
              {i > 0 && <span className="as-crumb-sep" aria-hidden="true"><Icon name="arrow" width={13} height={13} /></span>}
              {c.to && i < resolved.breadcrumb.length - 1 ? <Link to={c.to}>{i === resolved.breadcrumb.length - 1 ? title : c.label}</Link> : <span aria-current={i === resolved.breadcrumb.length - 1 ? 'page' : undefined}>{i === resolved.breadcrumb.length - 1 ? title : c.label}</span>}
            </span>)}
          </nav>
          <div className="as-topbar-right">
            {env?.mode === 'preview' && <span className="as-env" role="status"><Icon name="preview" width={14} height={14} /> {env.label || '演练环境'}</span>}
            <span className="as-avatar as-avatar-sm" aria-hidden="true">{initial}</span>
          </div>
        </header>

        {resolved.group && GROUP_TABS[resolved.group] && (
          <nav className="as-subnav" aria-label="页内导航">
            {GROUP_TABS[resolved.group].filter(t => !t.adminOnly || isAdmin).map(t => (
              <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `as-subnav-tab ${isActive ? 'is-active' : ''}`}>{t.label}</NavLink>
            ))}
          </nav>
        )}
        <main id="as-content" className="as-content" tabIndex={-1}>
          <div className="wb-shell as-pages">
            <WorkTitleContext.Provider value={setWorkTitle}>
              <Suspense fallback={<div className="cv-loading"><span className="cv-spinner" />正在打开…</div>}><Outlet /></Suspense>
            </WorkTitleContext.Provider>
          </div>
        </main>
      </div>

      {drawerOpen && <>
        <button className="as-scrim" aria-label="关闭导航" onClick={() => setDrawerOpen(false)} />
        <aside className="as-drawer" ref={drawerRef} aria-label="工作区导航" role="dialog" aria-modal="true">
          <div className="as-drawer-head"><span className="as-brand-mark">webuddy</span>
            <button className="as-drawer-close" aria-label="关闭导航" onClick={() => setDrawerOpen(false)}><Icon name="plus" width={18} height={18} style={{ transform: 'rotate(45deg)' }} /></button></div>
          {renderNav(() => setDrawerOpen(false))}
          {footer(() => setDrawerOpen(false))}
        </aside>
      </>}
    </div>
  )
}
