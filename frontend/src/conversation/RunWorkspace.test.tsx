// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import RunWorkspace from './RunWorkspace'
import { WorkTitleContext } from './title-context'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

function mount(run: Record<string, unknown>, messages: unknown[] = []) {
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url === '/api/v2/runs/r1' && (!opts || !opts.method)) return run as never
    if (url === '/api/v2/runs/r1/conversation') return { messages } as never
    if (typeof url === 'string' && url.endsWith('/deliverables')) return { items: [], can_collect: true } as never
    return { ok: true, id: 'r2' } as never
  })
  return render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
}
const base = { id: 'r1', project_id: 'p1', request: '做预约工具', revision: 1, plan: { title: '预约管理工具', summary: '', questions: [], tasks: [] }, artifacts: {}, created_at: '', updated_at: '' }
beforeEach(() => api.mockReset())
afterEach(cleanup)

it('shows the real progress head for an active run and queues a follow-up instead of dropping it', async () => {
  mount({ ...base, status: 'running' })
  await screen.findByText('正在处理')
  fireEvent.change(screen.getByLabelText('补充或修改'), { target: { value: '再加一个搜索' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await waitFor(() => expect(api.mock.calls.some(([u, o]) => u === '/api/v2/runs/r1/follow-up' && (o as { method?: string })?.method === 'POST')).toBe(true))
  const call = api.mock.calls.find(([u]) => u === '/api/v2/runs/r1/follow-up')
  expect((call?.[1] as { body?: { content?: string } })?.body?.content).toBe('再加一个搜索')
})

it('routes a clarification answer to the clarify endpoint', async () => {
  mount({ ...base, status: 'needs_clarification', plan: { title: '预约管理工具', summary: '', questions: ['需要哪些字段？'], tasks: [] } })
  await screen.findByText('等待你补充')
  fireEvent.change(screen.getByLabelText('补充或修改'), { target: { value: '客户名和时间' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await waitFor(() => expect(api.mock.calls.some(([u, o]) => u === '/api/v2/runs/r1/clarify' && (o as { method?: string })?.method === 'POST')).toBe(true))
})
