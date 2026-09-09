import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import type { User } from './workspace/types'
import { request, WorkspaceApiError } from './workspace/api'
import Workbench from './workbench/Workbench'
import OverviewPage from './workbench/OverviewPage'
import ProjectsPage from './workbench/ProjectsPage'
import ProjectPage from './workbench/ProjectPage'
import RunsPage from './workbench/RunsPage'
import CapabilitiesPage from './workbench/CapabilitiesPage'
import CostsPage from './workbench/CostsPage'
import TeamPage from './workbench/TeamPage'
import AgentsPage from './workbench/AgentsPage'
import type { PageProps } from './workbench/ui'
import './workbench/workbench.css'

const RunPage = lazy(() => import('./workbench/RunPage'))
const RuntimePage = lazy(() => import('./workbench/RuntimePage'))

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
    try { await onLogin(username.trim(), password) } catch (cause) { setError(cause instanceof Error ? cause.message : '登录失败，请重试。') } finally { setBusy(false) }
  }
  return <main className="wb-auth-page"><div className="wb-auth-aside"><div className="wb-brand-lockup wb-brand-large"><span className="wb-brand-mark" aria-hidden="true">w</span><span><strong>webuddy</strong><small>工程工作台</small></span></div><div className="wb-auth-message"><span className="wb-eyebrow">控制平面</span><h1>把工程工作<br />交给清晰的流程。</h1><p>需求、项目上下文、执行记录和交付证据，在一个工作区里保持可追溯。</p></div></div><section className="wb-auth-panel"><form className="wb-auth-card" onSubmit={submit}><span className="wb-eyebrow">欢迎回来</span><h2>登录工作台</h2><p>使用工作台账号继续。</p><label>用户名<input required autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} /></label><label>密码<input required type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>{error && <div className="wb-error" role="alert"><strong>登录失败</strong><span>{error}</span></div>}<button className="wb-button wb-button-primary wb-auth-submit" disabled={busy}>{busy ? '登录中…' : '进入工作台 →'}</button></form><small className="wb-auth-footnote">这是受保护的工程工作台。</small></section></main>
}

function LoadingPage() {
  return <main className="wb-auth-page wb-auth-loading"><div className="wb-auth-card"><span className="wb-spinner" aria-hidden="true" /><p>正在验证登录状态…</p></div></main>
}

function AuthGate({ children }: { children: (session: AuthResponse, logout: () => void) => ReactNode }) {
  const [session, setSession] = useState<AuthResponse | null>(null)
  const [checking, setChecking] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const sessionController = useRef<AbortController | null>(null)

  const checkSession = useCallback(() => {
    sessionController.current?.abort()
    const controller = new AbortController()
    sessionController.current = controller
    setChecking(true); setError(null)
    return request<AuthResponse>('/api/auth/me', { signal: controller.signal }).then((next) => {
      if (!controller.signal.aborted) setSession(next)
    }).catch((cause) => {
      if (controller.signal.aborted) return
      setSession(null)
      if (!(cause instanceof WorkspaceApiError && cause.status === 401)) setError(cause instanceof Error ? cause.message : '无法连接认证服务。')
    }).finally(() => { if (!controller.signal.aborted) setChecking(false) })
  }, [])
  useEffect(() => { void checkSession(); return () => sessionController.current?.abort() }, [checkSession])

  const login = async (username: string, password: string) => {
    const next = await request<AuthResponse>('/api/auth/login', { method: 'POST', body: { username, password } })
    setSession(next); setError(null)
  }
  const logout = useCallback(() => {
    const current = session
    setSession(null)
    if (current) void request('/api/auth/logout', { method: 'POST', csrfToken: current.csrf_token }).catch(() => undefined)
  }, [session])
  if (checking) return <LoadingPage />
  if (!session) return error ? <main className="wb-auth-page wb-auth-loading"><div className="wb-auth-card"><span className="wb-eyebrow">控制平面</span><h2>暂时无法连接</h2><p>{error}</p><button className="wb-button wb-button-primary" onClick={() => void checkSession()}>重新连接</button></div></main> : <LoginPage onLogin={login} />
  return <>{children(session, logout)}</>
}

function RoutedWorkbench({ session, logout }: { session: AuthResponse; logout: () => void }) {
  const pageProps: PageProps = { csrfToken: session.csrf_token, onUnauthorized: logout, user: session.user }
  const isAdmin = session.user.role !== 'member'
  return <Workbench user={session.user} onLogout={logout} {...pageProps}><Suspense fallback={<div className="wb-page"><div className="wb-card wb-loading-card"><span className="wb-spinner" aria-hidden="true" />正在打开页面…</div></div>}><Routes><Route index element={<AgentsPage {...pageProps} />} /><Route path="agents" element={<AgentsPage {...pageProps} />} /><Route path="agents/:agentId" element={<AgentsPage {...pageProps} />} /><Route path="overview" element={<OverviewPage {...pageProps} />} /><Route path="runs" element={<RunsPage {...pageProps} />} /><Route path="projects" element={<ProjectsPage {...pageProps} />} /><Route path="projects/:projectId" element={<ProjectPage {...pageProps} />} /><Route path="capabilities" element={<CapabilitiesPage {...pageProps} />} /><Route path="costs" element={<CostsPage {...pageProps} />} /><Route path="team" element={<TeamPage {...pageProps} />} /><Route path="runs/:runId" element={<RunPage {...pageProps} />} />{isAdmin && <Route path="settings/runtime" element={<RuntimePage {...pageProps} />} />}<Route path="*" element={<Navigate to="/" replace />} /></Routes></Suspense></Workbench>
}

export default function App() {
  return <BrowserRouter><AuthGate>{(session, logout) => <RoutedWorkbench session={session} logout={logout} />}</AuthGate></BrowserRouter>
}
