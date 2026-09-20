// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import AppShell from './AppShell'
import { resolveRoute } from './nav-config'
import { request } from '../workspace/api'
import type { WorkbenchProps } from '../workbench/ui'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const admin: WorkbenchProps = { csrfToken: 'x', onUnauthorized: vi.fn(), onLogout: vi.fn(), user: { id: 1, username: 'owner', role: 'admin' } }
function shell(path: string, props: WorkbenchProps = admin) {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/*" element={<AppShell {...props} />} />
  </Routes></MemoryRouter>)
}
beforeEach(() => { api.mockResolvedValue({ mode: 'live' } as never); localStorage.clear() })
afterEach(cleanup)

it('routes /runs/:id to 历史作品 and /runs to 工程总览 — never the same selection', () => {
  expect(resolveRoute('/runs/abc').activeKey).toBe('history')
  expect(resolveRoute('/runs').activeKey).toBe('engineering')
  expect(resolveRoute('/runs/abc').title).toBe('任务')
  expect(resolveRoute('/projects/p1').activeKey).toBe('engineering')
  expect(resolveRoute('/agents/a1').activeKey).toBe('agents')
  expect(resolveRoute('/settings/runtime').activeKey).toBe('settings')
})

it('shows the same five-item global nav on every route, marking the active one', () => {
  for (const [path, label] of [['/', '开始制作'], ['/history', '历史作品'], ['/agents', '职能体'], ['/overview', '工程总览'], ['/settings', '设置']] as const) {
    cleanup(); shell(path)
    for (const item of ['开始制作', '历史作品', '职能体', '工程总览', '设置']) expect(screen.getByLabelText(item)).toBeTruthy()
    expect(screen.getByLabelText(label).getAttribute('aria-current')).toBe('page')
  }
})

it('persists the collapse preference across a remount', () => {
  shell('/overview')
  fireEvent.click(screen.getByRole('button', { name: '收起导航' }))
  expect(localStorage.getItem('webuddy:nav:collapsed')).toBe('1')
  cleanup(); shell('/overview')
  expect(screen.getByRole('button', { name: '展开导航' })).toBeTruthy()
})

it('renders engineering group tabs only on engineering routes', () => {
  shell('/overview')
  const subnav = document.querySelector('.as-subnav')!
  expect(within(subnav as HTMLElement).getByText('运行记录')).toBeTruthy()
  cleanup(); shell('/history')
  expect(document.querySelector('.as-subnav')).toBeNull()
})

it('hides admin-only group tabs from members', () => {
  const member: WorkbenchProps = { ...admin, user: { ...admin.user, role: 'member' } }
  shell('/overview', member)
  const subnav = document.querySelector('.as-subnav') as HTMLElement
  expect(within(subnav).queryByText('总览')).toBeTruthy()
  expect(within(subnav).queryByText('团队')).toBeNull()
  expect(within(subnav).queryByText('用量与预算')).toBeNull()
})

it('marks 历史作品 with aria-current on a task page, and the parent nav item on a detail page', () => {
  shell('/runs/abc')
  expect(screen.getByLabelText('历史作品').getAttribute('aria-current')).toBe('page')
  expect(screen.getByLabelText('开始制作').getAttribute('aria-current')).toBeNull()
  cleanup(); shell('/agents/a1')
  expect(screen.getByLabelText('职能体').getAttribute('aria-current')).toBe('page')
  // agents group now has a single tab (no 能力库); sub-nav only renders with > 1 tab
  expect(document.querySelector('.as-subnav')).toBeNull()
  // verify engineering still shows sub-nav with multiple tabs
  cleanup(); shell('/projects')
  expect(screen.getByLabelText('工程总览').getAttribute('aria-current')).toBe('page')
  const engTabs = document.querySelector('.as-subnav') as HTMLElement
  expect(within(engTabs).getByText('项目').getAttribute('aria-current')).toBe('page')
})

it('closes the mobile drawer and releases the scroll lock when the viewport grows to desktop', () => {
  let listener: (() => void) | null = null
  const mql = { matches: false, addEventListener: (_: string, cb: () => void) => { listener = cb }, removeEventListener: () => {} }
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(mql))
  shell('/overview')
  fireEvent.click(screen.getByRole('button', { name: '打开导航' }))
  expect(document.querySelector('.as-drawer')).toBeTruthy()
  expect(document.body.style.overflow).toBe('hidden')
  // Simulate crossing into desktop width.
  act(() => { mql.matches = true; listener?.() })
  expect(document.querySelector('.as-drawer')).toBeNull()
  expect(document.body.style.overflow).toBe('')
  vi.unstubAllGlobals()
})
