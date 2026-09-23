import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import Icon from '../workbench/Icon'
import BrandMark from '../workbench/BrandMark'
import { request } from '../workspace/api'
import type { WorkbenchProps } from '../workbench/ui'
import { PRIMARY_NAV, BUSINESS_PLUGIN_NAV, SETTINGS_NAV, GROUP_TABS, resolveRoute, type NavItem } from './nav-config'
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
  const [management, setManagement] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const hamburgerRef = useRef<HTMLButtonElement>(null)

  const resolved = useMemo(() => resolveRoute(location.pathname), [location.pathname])
  const title = resolved.usesWorkTitle && workTitle ? workTitle : resolved.title

  useEffect(() => { try { localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0') } catch { /* optional */ } }, [collapsed])
  useEffect(() => { setDrawerOpen(false); setMenuOpen(false); setWorkTitle(null) }, [location.pathname])
  // Crossing into desktop width must close the mobile drawer so its scroll lock
  // and focus trap can't linger once the sidebar is visible again.
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia('(min-width: 769px)')
    const onChange = () => { if (mq.matches) setDrawerOpen(false) }
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  useEffect(() => { const c = new AbortController(); request<{ mode?: string; label?: string }>('/api/v3/environment', { onUnauthorized: onLogout, signal: c.signal }).then(v => { if (!c.signal.aborted) setEnv(v) }).catch(() => undefined); return () => c.abort() }, [onLogout])
  // 决定是否显示"管理"入口：企业治理 v1 的负责人/管理员管理视图，普通成员看不到。
  useEffect(() => { const c = new AbortController(); request<{ management?: boolean }>('/api/v5/me/workspaces', { onUnauthorized: onLogout, signal: c.signal }).then(v => { if (!c.signal.aborted) setManagement(Boolean(v.management)) }).catch(() => undefined); return () => c.abort() }, [onLogout])

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

  // Active state comes from resolveRoute, not NavLink's URL match, so a task page
  // (/runs/:id) correctly marks 历史作品 with aria-current even though the URL differs.
  const navLink = (item: NavItem, onNavigate?: () => void) => {
    const active = resolved.activeKey === item.key
    return <Link key={item.key} to={item.to} onClick={onNavigate}
      className={`as-nav-item ${active ? 'is-active' : ''}`}
      aria-current={active ? 'page' : undefined} aria-label={item.label} title={collapsed ? item.label : undefined}>
      <span className="as-nav-icon"><Icon name={item.icon} /></span><span className="as-nav-text">{item.label}</span>
    </Link>
  }
  // 企业治理 v1：管理入口不在 nav-config 里（只读管理视图，非工作台业务插件），
  // 有管理查看范围（管理员或获授权成员）时才显示，普通成员导航保持不变。
  const managementActive = resolved.activeKey === 'management'
  const renderNav = (onNavigate?: () => void) => (
    <div className="as-nav-scroll">
      <nav className="as-nav" aria-label="主导航">{navItems.map(item => navLink(item, onNavigate))}</nav>
      <nav className="as-nav as-plugin-nav" aria-label="业务插件">
        <span className="as-plugin-heading" aria-hidden="true">业务插件</span>
        {BUSINESS_PLUGIN_NAV.map(item => navLink(item, onNavigate))}
      </nav>
      {management && <nav className="as-nav" aria-label="管理">
        <Link to="/management" onClick={onNavigate} className={`as-nav-item ${managementActive ? 'is-active' : ''}`}
          aria-current={managementActive ? 'page' : undefined} aria-label="管理" title={collapsed ? '管理' : undefined}>
          <span className="as-nav-icon"><Icon name="settings" /></span><span className="as-nav-text">管理</span>
        </Link>
      </nav>}
    </div>
  )

  const footer = (onNavigate?: () => void) => (
    <div className="as-side-footer">
      {navLink(SETTINGS_NAV, onNavigate)}
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
      <Link to="/" className="as-brand-mark" aria-label="webuddy 首页"><BrandMark className="as-brand-icon" /><span className="as-brand-wordmark">webuddy</span></Link>
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
          <BrandMark className="as-mobile-brand" />
          <nav className="as-crumbs" aria-label="面包屑">
            {resolved.breadcrumb.map((c, i) => <span className="as-crumb" key={`${c.label}-${i}`}>
              {i > 0 && <span className="as-crumb-sep" aria-hidden="true"><Icon name="arrow" width={13} height={13} /></span>}
              {(() => {
                const last = i === resolved.breadcrumb.length - 1
                // `live` segments carry a name only the page knows (the agent's own
                // name); fall back to the static label until it has loaded.
                const text = last ? title : c.live ? (workTitle || c.label) : c.label
                return c.to && !last ? <Link to={c.to}>{text}</Link>
                  : <span aria-current={last ? 'page' : undefined}>{text}</span>
              })()}
            </span>)}
          </nav>
          <div className="as-topbar-right">
            {env?.mode === 'preview' && <span className="as-env" role="status"><Icon name="preview" width={14} height={14} /> {env.label || '演练环境'}</span>}
            <span className="as-avatar as-avatar-sm" aria-hidden="true">{initial}</span>
          </div>
        </header>

        {(() => {
          // A strip with a single destination is not navigation, it is decoration:
          // since 能力库 stopped being a sibling of 职能体, the 职能体 group has one
          // tab and the strip would just repeat the page title.
          const tabs = resolved.group && resolved.showTabs !== false && GROUP_TABS[resolved.group]
            ? GROUP_TABS[resolved.group].filter(t => !t.adminOnly || isAdmin) : []
          return tabs.length > 1 && (
            <nav className="as-subnav" aria-label="页内导航">
              {tabs.map(t => (
                <Link key={t.to} to={t.to} className={`as-subnav-tab ${resolved.activeTab === t.to ? 'is-active' : ''}`} aria-current={resolved.activeTab === t.to ? 'page' : undefined}>{t.label}</Link>
              ))}
            </nav>
          )
        })()}
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
          <div className="as-drawer-head"><span className="as-brand-mark"><BrandMark className="as-brand-icon" /><span className="as-brand-wordmark">webuddy</span></span>
            <button className="as-drawer-close" aria-label="关闭导航" onClick={() => setDrawerOpen(false)}><Icon name="plus" width={18} height={18} style={{ transform: 'rotate(45deg)' }} /></button></div>
          {renderNav(() => setDrawerOpen(false))}
          {footer(() => setDrawerOpen(false))}
        </aside>
      </>}
    </div>
  )
}
