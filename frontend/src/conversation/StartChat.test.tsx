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

it('routes a clear development request through the server, then creates project + run', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '做一个预约管理工具' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v2/projects/create-workspace', '/api/v2/runs'])
  expect(api.mock.calls[0][1]?.body).toMatchObject({ text: '做一个预约管理工具' })
  expect(api.mock.calls[2][1]?.body).toMatchObject({ project_id: 'p1', operation: 'general', interaction_mode: 'automatic' })
  expect(localStorage.getItem('webuddy:start:goal')).toBeNull()
})

it('routes a daily task to a role chat: opens a conversation and sends, no run', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9', agent_name: '报价助手' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '帮客户算一批设备的报价' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('对话已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v4/agents/a9/conversations', '/api/v4/conversations/c1/messages'])
  expect(api.mock.calls[1][1]?.body).toMatchObject({ mode: 'do', project_id: null })
  expect(api.mock.calls[2][1]?.body).toMatchObject({ content: '帮客户算一批设备的报价' })
})

it('on clarify, shows the question and lets the user pick a role', async () => {
  api.mockResolvedValueOnce({ kind: 'clarify', question: '这是要开发，还是让助手处理？', roles: [{ agent_id: 'a9', name: '报价助手' }], offer_development: true } as never)
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '做个报价' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('这是要开发，还是让助手处理？')
  fireEvent.click(screen.getByRole('button', { name: '交给「报价助手」' }))
  await screen.findByText('对话已打开')
  expect(api.mock.calls[1][1]?.body).toMatchObject({ agent_id: 'a9' })
})

it('on unavailable (MFD without converter), shows an honest message and creates nothing', async () => {
  api.mockResolvedValueOnce({ kind: 'unavailable', detail: 'mfd_no_converter', reason: 'MFD→XML 转换器尚未接入', missing: ['可运行的转换器源码'] } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '把这个 mfd 转成 xml' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText(/MFD→XML 转换器尚未接入/)
  expect(screen.getByText('可运行的转换器源码')).toBeTruthy()
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route'])  // nothing created
  expect(localStorage.getItem('webuddy:start:goal')).toBe('把这个 mfd 转成 xml')  // text preserved
})

it('keeps the created workspace after a failed run and continues without re-routing', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockRejectedValueOnce(new Error('网络中断'))
    .mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '做一个预约管理工具' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('网络中断')
  expect(localStorage.getItem('webuddy:start:goal')).toBe('做一个预约管理工具')
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v2/projects/create-workspace', '/api/v2/runs', '/api/v2/runs'])
})

it('imports attached files as a project without routing', async () => {
  api.mockResolvedValueOnce({ project: { id: 'p2' } } as never).mockResolvedValueOnce({ id: 'r2' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '基于这些材料继续' } })
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['x'], 'sample.csv')] } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls[0][0]).toBe('/api/v2/projects/import-files')
  expect((api.mock.calls[0][1]?.body as FormData).getAll('files')).toHaveLength(1)
})
