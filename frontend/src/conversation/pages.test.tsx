// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import HistoryPage from './HistoryPage'
import AgentCatalog from './AgentCatalog'
import SettingsPage from './SettingsPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()
const props = { csrfToken: 'x', onUnauthorized: noop, user: { id: 1, username: 'owner', role: 'admin' as const } }
beforeEach(() => { api.mockReset() })
afterEach(cleanup)

it('history lists works with real status and links back to the run', async () => {
  api.mockResolvedValue({ runs: [
    { id: 'r1', project_id: 'p1', request: '做预约工具', status: 'ready_for_review', revision: 2, plan: { title: '预约管理工具' }, updated_at: '2026-09-16T10:00:00Z' },
    { id: 'r2', project_id: 'p1', request: '做订单应用', status: 'running', revision: 1, updated_at: '2026-09-15T10:00:00Z' },
  ] } as never)
  render(<MemoryRouter><HistoryPage {...props} /></MemoryRouter>)
  const link = await screen.findByText('预约管理工具')
  expect(link.closest('a')?.getAttribute('href')).toBe('/runs/r1')
  expect(screen.getByText('已验证')).toBeTruthy()
  expect(screen.getByText('执行中')).toBeTruthy()
})

it('agent catalog shows a matched pack badge instead of a plain initial', async () => {
  api.mockResolvedValue({ agents: [
    { id: 'a1', name: '老项目渐进改造', purpose: '维护已有系统', active_version: 1, builtin_pack: 'legacy-modernization' },
    { id: 'a2', name: '我的自定义岗位', purpose: '自定义', active_version: 1 },
  ] } as never)
  const { container } = render(<MemoryRouter><AgentCatalog {...props} /></MemoryRouter>)
  await screen.findByText('老项目渐进改造')
  // built-in pack renders an accented mark badge (svg), custom falls back to the name initial
  expect(container.querySelector('.wb-category-badge--mark svg')).toBeTruthy()
  expect(screen.getByText('我的自定义岗位')).toBeTruthy()
  // primary action is 开始对话 (chat), maintenance is de-emphasized — no active_version 已就绪 claim
  const chat = screen.getAllByText('开始对话')
  expect(chat.length).toBe(2)
  expect(chat[0].closest('a')?.getAttribute('href')).toBe('/agents/a1/chat')
  expect(screen.queryByText('已就绪')).toBeNull()
})

it('settings shows real account and env, no fabricated toggles', async () => {
  api.mockResolvedValue({ mode: 'preview', label: '演练' } as never)
  render(<MemoryRouter><SettingsPage {...props} onLogout={noop} /></MemoryRouter>)
  expect(await screen.findByText(/owner · 管理员/)).toBeTruthy()
  expect(screen.getByText('打开运行配置').closest('a')?.getAttribute('href')).toBe('/settings/runtime')
})

it('does not report connected when reading the environment fails', async () => {
  api.mockRejectedValue(new Error('offline'))
  render(<MemoryRouter><SettingsPage {...props} onLogout={noop} /></MemoryRouter>)
  expect(screen.getByText('正在检查…')).toBeTruthy()
  await screen.findByText('环境状态读取失败')
  expect(screen.queryByText('已连接')).toBeNull()
})
