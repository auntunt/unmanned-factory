// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import StartChat from './StartChat'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const show = () => render(<MemoryRouter><Routes>
  <Route path="/" element={<StartChat csrfToken="csrf" onUnauthorized={vi.fn()} />} />
  <Route path="/runs/:id" element={<div>工作区已打开</div>} />
  <Route path="/agents/:id/chat" element={<div>对话已打开</div>} />
</Routes></MemoryRouter>)
beforeEach(() => { localStorage.clear(); api.mockReset() })
afterEach(cleanup)
const type = (value: string) => fireEvent.change(screen.getByLabelText('需求'), { target: { value } })
const go = () => fireEvent.click(screen.getByRole('button', { name: '开始制作' }))

it('routes a development request through the server, then creates project + run', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show(); type('做一个预约管理工具'); go()
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v2/projects/create-workspace', '/api/v2/runs'])
  expect(api.mock.calls[0][1]?.body).toMatchObject({ text: '做一个预约管理工具', attachments: [] })
  expect(localStorage.getItem('webuddy:start:goal')).toBeNull()
})

it('routes a daily task to a role chat and navigates to that exact conversation, no run', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('对话已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v4/agents/a9/conversations', '/api/v4/conversations/c1/messages'])
  expect(api.mock.calls[2][1]?.body).toMatchObject({ content: '帮客户算一批设备的报价' })
})

it('sends UTF-8 text attachments into the role chat, without creating a project', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9', attach: true } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({} as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('按这个价格表报价')
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['a,b'], 'prices.csv')] } })
  go()
  await screen.findByText('对话已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v4/agents/a9/conversations', '/api/v4/conversations/c1/attachments', '/api/v4/conversations/c1/messages'])
  expect((api.mock.calls[0][1]?.body as { attachments: unknown[] }).attachments).toHaveLength(1)
})

it('imports files as a project only when the server routes to development', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ project: { id: 'p2' } } as never).mockResolvedValueOnce({ id: 'r2' } as never)
  show(); type('用这些材料做一个管理系统')
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['x'], 'sample.csv')] } })
  go()
  await screen.findByText('工作区已打开')
  expect(api.mock.calls[0][0]).toBe('/api/v4/route')
  expect(api.mock.calls[1][0]).toBe('/api/v2/projects/import-files')
})

it('shows an honest message for MFD without a converter and creates nothing', async () => {
  api.mockResolvedValueOnce({ kind: 'unavailable', detail: 'mfd_no_converter', reason: 'MFD→XML 转换器尚未接入', missing: ['可运行的转换器源码'] } as never)
  show(); type('把这个 mfd 转成 xml'); go()
  await screen.findByText(/MFD→XML 转换器尚未接入/)
  expect(screen.getByText('可运行的转换器源码')).toBeTruthy()
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route'])
  expect(localStorage.getItem('webuddy:start:goal')).toBe('把这个 mfd 转成 xml')
})

it('on clarify, shows the question and lets the user pick a role', async () => {
  api.mockResolvedValueOnce({ kind: 'clarify', question: '要开发还是让助手处理？', roles: [{ agent_id: 'a9', name: '报价助手' }], offer_development: true } as never)
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('做个报价'); go()
  await screen.findByText('要开发还是让助手处理？')
  fireEvent.click(screen.getByRole('button', { name: '交给「报价助手」' }))
  await screen.findByText('对话已打开')
  expect(api.mock.calls[1][1]?.body).toMatchObject({ agent_id: 'a9' })
})

it('a lost message response does not create a second conversation on retry', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockRejectedValueOnce(new Error('响应丢失'))
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('响应丢失')
  go() // retry without editing: reuse conversation c1 + same key, resend message
  await screen.findByText('对话已打开')
  const urls = api.mock.calls.map(([url]) => url)
  expect(urls.filter(u => u === '/api/v4/agents/a9/conversations')).toHaveLength(1) // no duplicate conversation
  const keys = api.mock.calls.filter(([u]) => u === '/api/v4/conversations/c1/messages').map(([, o]) => (o?.body as { idempotency_key: string }).idempotency_key)
  expect(keys).toHaveLength(2) // message resent
  expect(keys[0]).toBe(keys[1]) // same idempotency key both times
})

it('keeps the created workspace after a failed run and continues without re-routing', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockRejectedValueOnce(new Error('网络中断'))
    .mockResolvedValueOnce({ id: 'r1' } as never)
  show(); type('做一个预约管理工具'); go()
  await screen.findByText('网络中断')
  go()
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v2/projects/create-workspace', '/api/v2/runs', '/api/v2/runs'])
})
