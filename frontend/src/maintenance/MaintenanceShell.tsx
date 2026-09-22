import { NavLink, Outlet } from 'react-router-dom'
import Icon from '../workbench/Icon'
import './subsystem.css'

/** The subsystem's own three-tab frame. It is mounted INSIDE the main webuddy
 *  AppShell (as a nested route's element) and also reused, without AppShell,
 *  by EmbeddedMaintenance for other hosts — so every link here is built from
 *  `basePath` rather than a hardcoded '/maintenance'. */
export default function MaintenanceShell({ basePath = '/maintenance' }: { basePath?: string }) {
  const tabClass = ({ isActive }: { isActive: boolean }) => `ms-tab ${isActive ? 'is-active' : ''}`
  const linkClass = ({ isActive }: { isActive: boolean }) => `ms-secondary-link ${isActive ? 'is-active' : ''}`
  return (
    <div className="ms-shell">
      <header className="ms-header">
        <div className="ms-title-row">
          <span className="ms-icon" aria-hidden="true"><Icon name="runs" /></span>
          <div>
            <nav className="ms-breadcrumb" aria-label="面包屑">
              <span>webuddy</span>
              <span className="ms-crumb-sep" aria-hidden="true">/</span>
              <span>运维维护子系统</span>
            </nav>
            <h1>运维维护</h1>
          </div>
        </div>
        <div className="ms-secondary-links">
          <NavLink to={`${basePath}/tasks`} className={linkClass}>全部任务</NavLink>
          <NavLink to={`${basePath}/about`} className={linkClass}>技术详情</NavLink>
        </div>
      </header>
      <nav className="ms-tabs" aria-label="子系统导航">
        <NavLink to={basePath} end className={tabClass}>运维监控</NavLink>
        <NavLink to={`${basePath}/repos`} className={tabClass}>维护代码库</NavLink>
        <NavLink to={`${basePath}/intake`} className={tabClass}>需求接入</NavLink>
      </nav>
      <div className="ms-content">
        <Outlet />
      </div>
    </div>
  )
}
