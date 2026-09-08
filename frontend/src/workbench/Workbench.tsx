import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'

import type { WorkbenchProps } from './ui'

const navigation = [
  { to: '/', label: '工作台', icon: '⌂', end: true },
  { to: '/projects', label: '项目', icon: '▦', end: false },
  { to: '/settings/runtime', label: '运行配置', icon: '◌', end: false },
]

export default function Workbench({ user, onLogout, children }: WorkbenchProps & { children?: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()

  useEffect(() => { setNavOpen(false) }, [location.pathname])

  return (
    <div className="wb-shell">
      <button className={`wb-mobile-scrim ${navOpen ? 'is-visible' : ''}`} aria-label="关闭导航" onClick={() => setNavOpen(false)} />
      <aside className={`wb-sidebar ${navOpen ? 'is-open' : ''}`}>
        <div className="wb-brand-lockup">
          <span className="wb-brand-mark" aria-hidden="true">F</span>
          <span><strong>无人工厂</strong><small>工程工作台</small></span>
        </div>
        <nav className="wb-nav" aria-label="主导航">
          <span className="wb-nav-label">工作区</span>
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `wb-nav-item ${isActive ? 'is-active' : ''}`}>
              <span className="wb-nav-icon" aria-hidden="true">{item.icon}</span><span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="wb-sidebar-footer">
          <div className="wb-sidebar-note"><span className="wb-note-dot" />受保护的工程工作台</div>
          <div className="wb-account">
            <span className="wb-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
            <span className="wb-account-name">{user.username}</span>
            <button className="wb-icon-button" onClick={onLogout} aria-label="退出登录" title="退出登录">↗</button>
          </div>
        </div>
      </aside>
      <div className="wb-main">
        <header className="wb-mobile-header">
          <button className="wb-menu-button" aria-label="打开导航" aria-expanded={navOpen} onClick={() => setNavOpen(true)}>☰</button>
          <span className="wb-mobile-title">无人工厂 <em>/</em> {navigation.find((item) => item.to === location.pathname)?.label ?? '项目'}</span>
          <span className="wb-avatar wb-avatar-small" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
        </header>
        <main className="wb-content">{children ?? <Outlet />}</main>
      </div>
    </div>
  )
}
