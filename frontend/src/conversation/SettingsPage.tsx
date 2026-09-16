import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { request } from '../workspace/api'
import type { WorkbenchProps } from '../workbench/ui'
import Icon from '../workbench/Icon'
import ThemeSwitch from '../workbench/ThemeSwitch'
import './conversation.css'

/** 设置：低频配置。只呈现有真实来源的项，不放不落库的假开关。
 *  模型、工具、权限等运行配置仍在「运行配置」页（管理员），此处给入口。 */
export default function SettingsPage({ user }: WorkbenchProps) {
  const isAdmin = user.role !== 'member'
  const [env, setEnv] = useState<{ mode?: string; label?: string } | null>(null)
  const [envError, setEnvError] = useState(false)
  useEffect(() => { request<{ mode?: string; label?: string }>('/api/v3/environment').then(setEnv).catch(() => setEnvError(true)) }, [])
  return (
    <div className="cv-page">
      <Link to="/" className="cv-page-back"><Icon name="back" width={16} height={16} /> 返回对话</Link>
      <h1>设置</h1>
      <p className="cv-page-sub">低频配置。日常制作会自动沿用这里的设定。</p>

      <section className="cv-settings-section">
        <h2>运行环境</h2>
        <div className="cv-settings-row">
          <div className="cv-sr-label">当前环境<small>制作在隔离工作区中进行，验证结果来自真实运行状态。</small></div>
          <div className="cv-sr-value"><span className={env && !envError ? 'cv-online' : ''}>{env && !envError && <i />}{envError ? '环境状态读取失败' : !env ? '正在检查…' : env.label || (env.mode === 'preview' ? '演练' : env.mode === 'live' ? '已连接' : '状态未知')}</span></div>
        </div>
        <div className="cv-settings-row">
          <div className="cv-sr-label">模型与工具<small>模型、阶段配置、工具与预算在运行配置中维护。</small></div>
          <div className="cv-sr-value">{isAdmin ? <Link to="/settings/runtime">打开运行配置 <Icon name="arrow" width={14} height={14} /></Link> : <span>由管理员维护</span>}</div>
        </div>
      </section>

      <section className="cv-settings-section">
        <h2>外观</h2>
        <div className="cv-settings-row">
          <div className="cv-sr-label">主题<small>跟随系统，或固定浅色 / 深色。</small></div>
          <div className="cv-sr-value"><ThemeSwitch /></div>
        </div>
      </section>

      <section className="cv-settings-section">
        <h2>账号</h2>
        <div className="cv-settings-row">
          <div className="cv-sr-label">当前账号</div>
          <div className="cv-sr-value">{user.username} · {isAdmin ? '管理员' : '成员'}</div>
        </div>
        {isAdmin && <div className="cv-settings-row">
          <div className="cv-sr-label">成员与权限</div>
          <div className="cv-sr-value"><Link to="/team">团队管理 <Icon name="arrow" width={14} height={14} /></Link></div>
        </div>}
      </section>
    </div>
  )
}
