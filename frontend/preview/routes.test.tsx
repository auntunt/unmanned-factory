// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RoutedWorkbench } from '../src/App'
import { request } from './api-mock'
vi.mock('../src/workspace/api', () => import('./api-mock'))
afterEach(cleanup)
const session = { csrf_token: 'preview', user: { id: 1, username: 'owner', role: 'admin' as const } }
const logout = () => {}
it.each([
  ['/overview', '工程总览'], ['/runs', '运行看板'], ['/projects', '项目'],
  ['/costs', '用量与预算'], ['/team', '团队'], ['/settings', '设置'],
  ['/history', '历史作品'], ['/agents', '职能体'], ['/settings/runtime', '运行配置'],
])('renders the real %s route with preview data', async (path, title) => {
  render(<MemoryRouter initialEntries={[path]}><RoutedWorkbench session={session} logout={logout} /></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: title, level: 1 })).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})
it('refuses preview writes and missing fixtures explicitly', async () => {
  await expect(request('/api/v2/runs', { method: 'POST' })).rejects.toThrow('没有保存或执行')
  await expect(request('/api/unknown')).rejects.toThrow('尚无预览样例')
})

it('keeps one shell mounted across navigation instead of swapping frames', async () => {
  const { container } = render(<MemoryRouter initialEntries={['/overview']}><RoutedWorkbench session={session} logout={logout} /></MemoryRouter>)
  await screen.findByRole('heading', { name: '工程总览', level: 1 })
  const shell = container.querySelector('.as-app'); const side = container.querySelector('.as-side')
  expect(shell).toBeTruthy()
  fireEvent.click(screen.getByLabelText('历史作品'))
  await screen.findByRole('heading', { name: '历史作品', level: 1 })
  // The layout element is the SAME node — the frame was not remounted.
  expect(container.querySelector('.as-app')).toBe(shell)
  expect(container.querySelector('.as-side')).toBe(side)
  fireEvent.click(screen.getByLabelText('设置'))
  await screen.findByRole('heading', { name: '设置', level: 1 })
  expect(container.querySelector('.as-app')).toBe(shell)
})
