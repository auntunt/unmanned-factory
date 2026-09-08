import { useCallback, useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { Alert, Layout, Result, Select, Space, Tabs, Tag } from 'antd'
import ClosurePage from './pages/ClosurePage'
import Overview from './pages/Overview'
import GatesPage from './pages/GatesPage'
import TaskList from './pages/TaskList'
import TaskDetail from './pages/TaskDetail'
import Submit from './pages/Submit'
import Stats from './pages/Stats'
import NotAvailable from './pages/NotAvailable'
import type { TaskRow } from './components/FactoryTaskCard'
import type { ApiTasks } from './lib/bucket'
import { C, FONT } from './theme/tokens'
import ControlRoom from './controlroom/ControlRoom'
import Workspace from './workspace/Workspace'
import { request, WorkspaceApiError } from './workspace/api'
import type { User } from './workspace/types'

/**
 * 应用外壳 —— 移植自 OA 闭环监控页：卡片式页签 + 单页无路由跳转。
 *
 * 换掉原来的 react-router 多页结构：那套每切一次页面就整屏白一下，
 * 而这是个要长期挂在屏幕上盯的看板，页签切换必须无闪烁。
 * 任务详情是唯一的例外（要占满整屏），用状态而非路由驱动。
 */

const WINDOWS = [
  { value: 7, label: '近 7 天' },
  { value: 14, label: '近 14 天' },
  { value: 30, label: '近 30 天' },
  { value: 90, label: '近 90 天' },
]

/**
 * BrowserRouter 仍然留着：TaskList / Submit / TaskDetail 里用了 Link 和
 * useNavigate，没有 Router 祖先会直接抛「useNavigate() may be used only in
 * the context of a Router」白屏。页签导航不走路由，但这些页面内部的跳转要。
 */
/**
 * 三个入口：
 *   /            新工程工作台
 *   /control-room 控制室 —— 旧的单屏事件视图
 *   /admin/*     原来的多页签管理台
 */
interface AuthResponse {
  user: User
  csrf_token: string
}

function LoginPage({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(null); setBusy(true)
    try { await onLogin(username.trim(), password) } catch (cause) { setError(cause instanceof Error ? cause.message : '登录失败') } finally { setBusy(false) }
  }
  return <main className="wf-login-page"><form className="wf-login-card" onSubmit={submit}><h1>工程工作台</h1><p>使用控制平面账号登录，查看规划、执行和交付证据。</p><div className="wf-field"><label htmlFor="login-username">用户名</label><input id="login-username" required autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} /></div><div className="wf-field" style={{ marginTop: 12 }}><label htmlFor="login-password">密码</label><input id="login-password" required type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></div>{error && <div className="wf-error" role="alert">{error}</div>}<div className="wf-form-actions" style={{ marginTop: 19 }}><button className="wf-button wf-button-primary" disabled={busy}>{busy ? '登录中…' : '登录'}</button></div></form></main>
}

function AuthGate({ children }: { children: (session: AuthResponse, logout: () => void) => ReactNode }) {
  const [session, setSession] = useState<AuthResponse | null>(null)
  const [checking, setChecking] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    request<AuthResponse>('/api/auth/me').then((next) => { if (!cancelled) { setSession(next); setError(null) } }).catch((cause) => { if (!cancelled && !(cause instanceof WorkspaceApiError && cause.status === 401)) setError(cause instanceof Error ? cause.message : '无法连接认证服务') }).finally(() => { if (!cancelled) setChecking(false) })
    return () => { cancelled = true }
  }, [])
  const login = async (username: string, password: string) => {
    const next = await request<AuthResponse>('/api/auth/login', { method: 'POST', body: { username, password } })
    setSession(next); setError(null)
  }
  const logout = useCallback(() => {
    const current = session
    setSession(null)
    if (current) void request('/api/auth/logout', { method: 'POST', csrfToken: current.csrf_token }).catch(() => undefined)
  }, [session])
  if (checking) return <main className="wf-login-page"><div className="wf-login-card"><p>正在验证登录状态…</p></div></main>
  if (!session) return error ? <main className="wf-login-page"><div className="wf-login-card"><h1>暂时无法登录</h1><p>{error}</p><button className="wf-button wf-button-primary" onClick={() => { setError(null); setChecking(true); window.location.reload() }}>重试</button></div></main> : <LoginPage onLogin={login} />
  return <>{children(session, logout)}</>
}

export default function App() {
  const path = window.location.pathname
  const admin = path.startsWith('/admin')
  return <BrowserRouter basename={admin ? '/admin' : undefined}><AuthGate>{(session, logout) => path.startsWith('/admin') ? <FactoryConsole /> : path.startsWith('/control-room') ? <ControlRoom /> : <Workspace user={session.user} csrfToken={session.csrf_token} onLogout={logout} />}</AuthGate></BrowserRouter>
}

// 不叫 Console：和全局 console 混淆，也和旧的 pages/Console 看板页重名。
function FactoryConsole() {
  const [tab, setTab] = useState('closure')
  const [days, setDays] = useState(30)
  const [tasks, setTasks] = useState<ApiTasks | null>(null)
  const [analytics, setAnalytics] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [openTask, setOpenTask] = useState<string | null>(null)

  const load = useCallback(async () => {
    setErr(null)
    try {
      const [t, a] = await Promise.all([
        fetch('/api/tasks').then((r) => (r.ok ? r.json() : Promise.reject(new Error(`/api/tasks ${r.status}`)))),
        fetch(`/api/analytics?days=${days}`).then((r) =>
          r.ok ? r.json() : Promise.reject(new Error(`/api/analytics ${r.status}`)),
        ),
      ])
      setTasks(t)
      setAnalytics(a)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    load()
    // 30s 轮询：worker 一轮跑几分钟，再快没有新信息，只是白耗请求。
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [load])

  const onTaskClick = (t: TaskRow) => setOpenTask(t.task_id)

  if (openTask) {
    return (
      <Layout style={{ minHeight: '100vh', background: C.bgPage, fontFamily: FONT }}>
        <TaskDetail taskId={openTask} onBack={() => setOpenTask(null)} />
      </Layout>
    )
  }

  const items = [
    {
      key: 'closure',
      label: '闭环链路',
      children: (
        <ClosurePage
          tasks={tasks}
          analytics={analytics}
          loading={loading}
          onTaskClick={onTaskClick}
        />
      ),
    },
    {
      // Overview / TaskList / Submit / Stats 都是已在跑的自取数页面，
      // 各自带 usePolling。外壳不给它们喂数据，避免同一份数据两条取数路径
      // 打架（那会让两个页签显示不同的数，且很难查）。
      key: 'overview',
      label: '总览分析',
      children: <Overview />,
    },
    {
      key: 'gates',
      label: '判据闸门',
      children: <GatesPage analytics={analytics} loading={loading} />,
    },
    {
      key: 'tasks',
      label: '任务列表',
      children: <TaskList />,
    },
    {
      key: 'submit',
      label: '投递任务',
      children: <Submit />,
    },
    {
      key: 'supervisors',
      label: '监工统计',
      children: <Stats />,
    },
    {
      key: 'sla',
      label: 'SLA 达成',
      children: (
        <NotAvailable
          title="SLA 达成率"
          why="工厂里没有 SLA 这个概念 —— 任务没有承诺完成时限，也没有紧急程度分级。"
          instead="要看时效，用「总览分析」里的滞留任务表和平均轮次；要看卡点，看「闭环链路」的待人介入环。"
        />
      ),
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh', background: C.bgPage, fontFamily: FONT }}>
      <Layout.Header
        style={{
          background: '#fff',
          borderBottom: `1px solid ${C.borderLight}`,
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          height: 56,
          lineHeight: 'normal',
        }}
      >
        <span style={{ fontSize: 16, fontWeight: 600, color: C.text }}>自动化无人工厂</span>
        <Tag bordered={false} color="blue" style={{ fontSize: 11 }}>
          闭环监控
        </Tag>
        <div style={{ flex: 1 }} />
        <Space>
          {analytics?.generated_at && (
            <span style={{ fontSize: 11, color: C.textDisabled }}>
              数据时间 {analytics.generated_at.replace('T', ' ')}
            </span>
          )}
          <Select
            size="small"
            value={days}
            options={WINDOWS}
            onChange={setDays}
            style={{ width: 110 }}
          />
        </Space>
      </Layout.Header>

      <Layout.Content style={{ padding: '16px 24px 32px' }}>
        {err ? (
          <Result
            status="error"
            title="接口请求失败"
            subTitle={
              <div style={{ fontSize: 12, color: C.textSub }}>
                <code>{err}</code>
                <div style={{ marginTop: 8 }}>
                  这是「请求失败」，不是「没有数据」—— 页面上任何 0 都不该按真实值解读。
                </div>
              </div>
            }
          />
        ) : (
          <>
            {analytics?.empty && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="所选窗口内没有任何执行记录"
                description="接口通了、库也读到了，确实是这段时间没跑任务。换个更长的时间窗看看。"
              />
            )}
            <Tabs
              type="card"
              activeKey={tab}
              onChange={setTab}
              items={items}
              destroyInactiveTabPane={false}
            />
          </>
        )}
      </Layout.Content>
    </Layout>
  )
}
