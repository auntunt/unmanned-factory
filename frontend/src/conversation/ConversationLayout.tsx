import { useEffect, useRef, useState } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import Icon from '../workbench/Icon'
import ThemeSwitch from '../workbench/ThemeSwitch'
import type { WorkbenchProps } from '../workbench/ui'
import { WorkTitleContext } from './title-context'
import './conversation.css'

/** Shell for the primary conversational flow. Brand centre-left, a work title
 *  when inside a run, and the two low-frequency entries (history + avatar menu)
 *  on the right. Management pages stay reachable through the avatar menu. */
export default function ConversationLayout({ user, onLogout }: WorkbenchProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [title, setTitle] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const location = useLocation()
  const isAdmin = user.role !== 'member'
  useEffect(() => setMenuOpen(false), [location.pathname])
  useEffect(() => { setTitle(null) }, [location.pathname])
  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (event: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false) }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('mousedown', onDoc); document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [menuOpen])
  const initial = Array.from(user.username || 'u')[0]?.toUpperCase() ?? 'U'
  return (
    <div className="cv-app">
      <header className={`cv-topbar${title ? ' is-titled' : ''}`}>
        <Link to="/" className="cv-brand">webuddy</Link>
        {title ? <div className="cv-title" title={title}>{title}</div> : <span />}
        <div className="cv-topbar-actions">
          <Link to="/history" className="cv-icon-button" aria-label="历史作品"><Icon name="history" /></Link>
          <div className="cv-menu-wrap" ref={menuRef}>
            <button className="cv-avatar" aria-haspopup="menu" aria-expanded={menuOpen} aria-label="账号菜单" onClick={() => setMenuOpen(open => !open)}>{initial}</button>
            {menuOpen && (
              <div className="cv-menu" role="menu">
                <small>{user.username}</small>
                <Link to="/history" role="menuitem"><Icon name="history" /> 历史作品</Link>
                <Link to="/agents" role="menuitem"><Icon name="agent" /> 职能体</Link>
                <div className="cv-menu-sep" />
                <Link to="/overview" role="menuitem"><Icon name="overview" /> 工程总览</Link>
                <Link to="/runs" role="menuitem"><Icon name="runs" /> 运行记录</Link>
                <Link to="/projects" role="menuitem"><Icon name="project" /> 我的作品</Link>
                {isAdmin && <Link to="/costs" role="menuitem"><Icon name="costs" /> 用量与预算</Link>}
                {isAdmin && <Link to="/team" role="menuitem"><Icon name="team" /> 团队</Link>}
                <Link to="/settings" role="menuitem"><Icon name="settings" /> 设置</Link>
                <div className="cv-menu-sep" />
                <div style={{ padding: '2px 4px' }}><ThemeSwitch /></div>
                <button role="menuitem" onClick={onLogout}><Icon name="logout" /> 退出登录</button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="cv-main"><WorkTitleContext.Provider value={setTitle}><Outlet /></WorkTitleContext.Provider></main>
    </div>
  )
}
