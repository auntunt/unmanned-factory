// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import CostsPage from './CostsPage'
import RunWorkspace from '../conversation/RunWorkspace'
import { WorkTitleContext } from '../conversation/title-context'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const overview = {
  runs: 1, known_cost_usd: 1, unknown_cost_runs: 0, model_usage: [],
  cost_policy: { revision: 3, default_project_budget_usd: null },
  project_summaries: [
    { id: 'p1', name: '跟随项目', budget_usd: null, budget_source: 'inherit', effective_budget_usd: null },
    { id: 'p2', name: '单独项目', budget_usd: 10, budget_source: 'explicit', effective_budget_usd: 10 },
  ],
}
const paused: Run = {
  id: 'r1', project_id: 'p1', request: '继续做报表', status: 'needs_human', revision: 4,
  created_at: '2026-09-20T01:00:00Z', updated_at: '2026-09-20T02:00:00Z',
  plan: { title: '报表', summary: '', tasks: [], questions: [] },
  artifacts: { budget_exhausted: true, needs_human: '模型调用已停止' },
  source: { actor_id: 7 },
} as unknown as Run
beforeEach(() => { api.mockReset(); api.mockImplementation(async () => ({}) as never) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

function showRun(role: 'admin' | 'member') {
  api.mockImplementation(async (url, opts) => {
    if (url === '/api/v2/runs/r1' && !opts?.method) return paused as never
    if (url === '/api/v2/runs/r1/conversation') return { messages: [] } as never
    if (url.endsWith('/deliverables')) return { items: [], can_collect: true } as never
    return {} as never
  })
  return render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={vi.fn()} pollMs={0} user={{ id: 7, username: 'seat', role }} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
}

it('offers the budget decision to an admin and an accurate explanation to the owning member', async () => {
  const view = showRun('admin')
  await screen.findByRole('button', { name: '续跑并进入下一阶段' })
  view.unmount(); cleanup()
  showRun('member')
  // The member owns this run, so only the role withholds the renewal; the server
  // answers 403 to it, and a button that can only ever fail must not be offered.
  expect(await screen.findByText(/提高额度是管理员的操作/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: '续跑并进入下一阶段' })).toBeNull()
  expect(screen.getByRole('link', { name: '查看本任务用量' })).toBeTruthy()
})

it('keeps the cost policy admin-only and saves monitoring as an explicit null', async () => {
  api.mockResolvedValue(overview as never)
  const member = render(<MemoryRouter><CostsPage csrfToken="csrf" onUnauthorized={vi.fn()} user={{ id: 7, username: 'seat', role: 'member' }} /></MemoryRouter>)
  await screen.findByText('跟随平台策略')
  expect(screen.queryByLabelText('平台费用策略')).toBeNull()
  member.unmount(); cleanup()
  render(<MemoryRouter><CostsPage csrfToken="csrf" onUnauthorized={vi.fn()} user={{ id: 1, username: 'owner', role: 'admin' }} /></MemoryRouter>)
  const form = await screen.findByLabelText('平台费用策略')
  expect((screen.getByLabelText('默认模式') as HTMLSelectElement).value).toBe('monitor')
  expect(form.textContent).toContain('已有项目的设置和现有停止线不会被改写')
  fireEvent.change(screen.getByLabelText('默认模式'), { target: { value: 'enforce' } })
  fireEvent.change(screen.getByLabelText('默认单次运行停止线（美元）'), { target: { value: '25' } })
  api.mockResolvedValueOnce({ revision: 4, default_project_budget_usd: 25 } as never)
  fireEvent.click(screen.getByRole('button', { name: '保存费用策略' }))
  await waitFor(() => expect(api.mock.calls.find(([url, options]) => url === '/api/v2/runtime/cost-policy' && options?.method === 'PUT')?.[1]?.body).toEqual({ revision: 3, default_project_budget_usd: 25 }))
  await screen.findByText('已保存。')
})

it('shows the resolved ceiling and its source per project', async () => {
  api.mockResolvedValue({ ...overview, cost_policy: { revision: 4, default_project_budget_usd: 30 }, project_summaries: [{ ...overview.project_summaries[0], effective_budget_usd: 30 }, overview.project_summaries[1]] } as never)
  render(<MemoryRouter><CostsPage csrfToken="csrf" onUnauthorized={vi.fn()} user={{ id: 1, username: 'owner', role: 'admin' }} /></MemoryRouter>)
  const inherited = (await screen.findByText('跟随项目')).closest('tr')!
  // The inheriting project reports the policy amount it actually runs under,
  // while the project that owns its ceiling keeps its own number.
  expect(inherited.textContent).toContain('每次运行 $30 后停止')
  expect(inherited.textContent).toContain('跟随平台策略')
  const explicit = screen.getByText('单独项目').closest('tr')!
  expect(explicit.textContent).toContain('每次运行 $10 后停止')
  expect(explicit.textContent).toContain('项目单独指定')
})
