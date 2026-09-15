import Icon from './Icon'
import ThemeSwitch from './ThemeSwitch'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { request } from '../workspace/api'

import type { WorkbenchProps } from './ui'

const navigation = [
  { to: '/agents', label: '职能体', icon: 'agent' as const, end: false },
  { to: '/', label: '工作总览', icon: 'overview' as const, end: true },
  { to: '/runs', label: '运行看板', icon: 'runs' as const, end: false },
  { to: '/projects', label: '项目', icon: 'project' as const, end: false },
  { to: '/ability-center', label: '能力中心', icon: 'modules' as const, end: false },
  { to: '/costs', label: '用量与预算', icon: 'costs' as const, end: false },
  { to: '/team', label: '团队', icon: 'team' as const, end: false },
]

const navOrder = ['/', '/projects', '/runs', '/ability-center', '/agents', '/costs', '/team']
navigation.sort((a, b) => navOrder.indexOf(a.to) - navOrder.indexOf(b.to))

const secondaryNavigation = [{ to: '/settings/runtime', label: '运行配置', icon: 'settings' as const, end: false }]


export default function Workbench({ user, onLogout, children }: WorkbenchProps & { children?: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)
  const [environment, setEnvironment] = useState<{ mode: 'preview' | 'live'; label: string } | null>(null)
  const location = useLocation()
  const visibleSecondary = user.role === 'member' ? [] : secondaryNavigation
  const currentPage = [...navigation, ...visibleSecondary].find((item) => item.end ? (location.pathname === item.to || (item.to === '/' && location.pathname === '/overview')) : location.pathname.startsWith(item.to))

  useEffect(() => { setNavOpen(false) }, [location.pathname])
  useEffect(() => { const controller = new AbortController(); void request<{ mode: 'preview' | 'live'; label: string }>('/api/v3/environment', { onUnauthorized: onLogout, signal: controller.signal }).then((value) => { if (!controller.signal.aborted) setEnvironment(value) }).catch(() => undefined); return () => controller.abort() }, [onLogout])

  return (
    <div className="wb-shell">
      <a className="wb-skip-link" href="#workbench-content">跳到主要内容</a>
      <button className={`wb-mobile-scrim ${navOpen ? 'is-visible' : ''}`} aria-label="关闭导航" onClick={() => setNavOpen(false)} />
      <aside className={`wb-sidebar ${navOpen ? 'is-open' : ''}`} aria-label="工作台导航">
        <div className="wb-brand-lockup">
          <span className="wb-brand-mark" aria-hidden="true">w</span>
          <span><strong>webuddy</strong><small>团队工作伙伴</small></span>
        </div>
        <nav className="wb-nav" aria-label="主导航">
          <span className="wb-nav-label">工作区</span>
          {navigation.map((item, index) => (
            <div key={item.to} className="wb-nav-entry">{index === 3 && <span className="wb-nav-label wb-nav-group">能力与经验</span>}{index === 5 && <span className="wb-nav-label wb-nav-group">团队管理</span>}<NavLink aria-label={item.label} aria-current={item.to === '/' && location.pathname === '/overview' ? 'page' : undefined} to={item.to} end={item.end} className={({ isActive }) => `wb-nav-item ${isActive ? 'is-active' : ''}`}>
              <span className="wb-nav-icon"><Icon name={item.icon} /></span><span>{item.label}</span>
            </NavLink></div>
          ))}
          {visibleSecondary.length > 0 && <span className="wb-nav-label wb-nav-label-secondary">系统</span>}
          {visibleSecondary.map((item) => <NavLink aria-label={item.label} key={item.to} to={item.to} end={item.end} className={({ isActive }) => `wb-nav-item ${isActive ? 'is-active' : ''}`}><span className="wb-nav-icon"><Icon name={item.icon} /></span><span>{item.label}</span></NavLink>)}
        </nav>
        <div className="wb-sidebar-footer">
          <ThemeSwitch /><div className="wb-sidebar-note">需求驱动 · 结果可追溯</div>
          <div className="wb-account">
            <span className="wb-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
            <span className="wb-account-name">{user.username}</span>
            <button className="wb-icon-button" onClick={onLogout} aria-label="退出登录" title="退出登录"><Icon name="logout" /></button>
          </div>
        </div>
      </aside>
      <div className="wb-main">
        <header className="wb-mobile-header">
          <button className="wb-menu-button" aria-label="打开导航" aria-expanded={navOpen} onClick={() => setNavOpen(true)}><Icon name="menu" /></button>
          <span className="wb-mobile-title">webuddy <em>/</em> {currentPage?.label ?? '工作区'}</span>
          <span className="wb-avatar wb-avatar-small" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
        </header>
        {environment?.mode === 'preview' && <div className="wb-preview-banner" role="status"><span aria-hidden="true"><Icon name="preview" /></span><strong>本地演练</strong><span>{environment.label || '使用脚本执行，不调用模型或外部服务'}</span></div>}
        <main id="workbench-content" className="wb-content" tabIndex={-1}>{children ?? <Outlet />}</main>
      </div>
    </div>
  )
}
