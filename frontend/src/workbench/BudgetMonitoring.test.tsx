// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import CostsPage from './CostsPage'
import TeamPage from './TeamPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('shows recorded cost, incomplete accounting, and direct project budget links', async () => {
  vi.mocked(request).mockResolvedValue({ runs: 2, known_cost_usd: 123.45, unknown_cost_runs: 1, model_usage: [], project_summaries: [{ id: 'p1', name: '监测项目', budget_usd: null }, { id: 'p2', name: '限制项目', budget_usd: 10 }] })
  render(<MemoryRouter><CostsPage csrfToken="csrf" onUnauthorized={vi.fn()} /></MemoryRouter>)
  expect(await screen.findByText('$123.4500')).toBeTruthy()
  expect(screen.getByText('1 条运行费用不完整')).toBeTruthy()
  expect(screen.getByText('仅监测')).toBeTruthy()
  expect(screen.getByText('每次运行 $10 后停止')).toBeTruthy()
  expect(screen.getByRole('link', { name: '管理 监测项目 预算' }).getAttribute('href')).toBe('/projects/p1?tab=settings#project-budget')
})
it('restores team quota visibility and unrestricted monthly control', async () => {
  const quota = { limit_tokens: null, used_tokens: 42, reserved_tokens: 0, unknown_calls: 0, remaining_tokens: null }
  vi.mocked(request).mockResolvedValue({ month: '2026-09', timezone: 'UTC', reservation_tokens: 20000, workspace: quota, members: [], projects: [], calls: [], audit: [] })
  render(<MemoryRouter><TeamPage user={{ id: 1, role: 'admin', username: 'owner' }} csrfToken="csrf" onUnauthorized={vi.fn()} /></MemoryRouter>)
  expect(await screen.findByText('工作区本月用量')).toBeTruthy()
  expect(screen.getByLabelText('工作区月额度')).toBeTruthy()
  expect((screen.getByLabelText('工作区月额度') as HTMLInputElement).value).toBe('')
})
