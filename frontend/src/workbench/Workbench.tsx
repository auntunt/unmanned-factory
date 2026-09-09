import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { request } from '../workspace/api'

import type { WorkbenchProps } from './ui'

const navigation = [
  { to: '/', label: '职能体', icon: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM4 21a8 8 0 0 1 16 0M19 5h2M20 4v2', end: true },
  { to: '/overview', label: '工程总览', icon: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z', end: true },
  { to: '/runs', label: '运行看板', icon: 'M4 4h16v16H4zM9 4v16M15 4v16M4 10h5M9 14h6M15 8h5', end: false },
  { to: '/projects', label: '项目', icon: 'M3 7V5h6l2 2h10v13H3z', end: false },
  { to: '/capabilities', label: '工作能力', icon: 'm12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5', end: false },
  { to: '/costs', label: '模型与成本', icon: 'M4 4v16h17M8 15v-4M13 15V7M18 15v-6', end: false },
  { to: '/team', label: '团队与额度', icon: 'M7 20v-2.5A3.5 3.5 0 0 1 10.5 14h3A3.5 3.5 0 0 1 17 17.5V20M12 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6M18 13a2.5 2.5 0 0 1 2 2.45V18M17 5.5a2.5 2.5 0 0 1 0 4.5', end: false },
]

const secondaryNavigation = [{ to: '/settings/runtime', label: '运行配置', icon: 'M4 7h16M4 17h16M8 4v6M16 14v6', end: false }]

function NavIcon({ path }: { path: string }) {
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg>
}

export default function Workbench({ user, onLogout, children }: WorkbenchProps & { children?: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)
  const [environment, setEnvironment] = useState<{ mode: 'preview' | 'live'; label: string } | null>(null)
  const location = useLocation()
  const visibleSecondary = user.role === 'member' ? [] : secondaryNavigation
  const currentPage = [...navigation, ...visibleSecondary].find((item) => item.end ? location.pathname === item.to : location.pathname.startsWith(item.to))

  useEffect(() => { setNavOpen(false) }, [location.pathname])
  useEffect(() => { const controller = new AbortController(); void request<{ mode: 'preview' | 'live'; label: string }>('/api/v3/environment', { onUnauthorized: onLogout, signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setEnvironment(value) }).catch(() => undefined); return () => controller.abort() }, [onLogout])

  return (
    <div className="wb-shell">
      <a className="wb-skip-link" href="#workbench-content">跳到主要内容</a>
      <button className={`wb-mobile-scrim ${navOpen ? 'is-visible' : ''}`} aria-label="关闭导航" onClick={() => setNavOpen(false)} />
      <aside className={`wb-sidebar ${navOpen ? 'is-open' : ''}`} aria-label="工作台导航">
        <div className="wb-brand-lockup">
          <span className="wb-brand-mark" aria-hidden="true">w</span>
          <span><strong>webuddy</strong><small>自主工程工作台</small></span>
        </div>
        <nav className="wb-nav" aria-label="主导航">
          <span className="wb-nav-label">工作区</span>
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `wb-nav-item ${isActive ? 'is-active' : ''}`}>
              <span className="wb-nav-icon"><NavIcon path={item.icon} /></span><span>{item.label}</span>
            </NavLink>
          ))}
          {visibleSecondary.length > 0 && <span className="wb-nav-label wb-nav-label-secondary">系统</span>}
          {visibleSecondary.map((item) => <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `wb-nav-item ${isActive ? 'is-active' : ''}`}><span className="wb-nav-icon"><NavIcon path={item.icon} /></span><span>{item.label}</span></NavLink>)}
        </nav>
        <div className="wb-sidebar-footer">
          <div className="wb-sidebar-note">需求驱动 · 结果可追溯</div>
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
          <span className="wb-mobile-title">webuddy <em>/</em> {currentPage?.label ?? '工作区'}</span>
          <span className="wb-avatar wb-avatar-small" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
        </header>
        {environment?.mode === 'preview' && <div className="wb-preview-banner" role="status"><span aria-hidden="true">◌</span><strong>本地演练</strong><span>{environment.label || '使用脚本执行，不调用模型或外部服务'}</span></div>}
        <main id="workbench-content" className="wb-content" tabIndex={-1}>{children ?? <Outlet />}</main>
      </div>
    </div>
  )
}
