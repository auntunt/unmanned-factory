// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RoutedWorkbench } from '../src/App'
import { request } from './api-mock'
vi.mock('../src/workspace/api', () => import('./api-mock'))
afterEach(cleanup)
const session = { csrf_token: 'preview', user: { id: 1, username: 'owner', role: 'admin' as const } }
const logout = () => {}
it.each([
  ['/overview', '工作总览'], ['/runs', '运行看板'], ['/projects', '项目'],
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
