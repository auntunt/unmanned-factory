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
</Routes></MemoryRouter>)
beforeEach(() => { localStorage.clear(); api.mockReset() })
afterEach(cleanup)

it('sends one goal and moves into the run workspace, no operation or agent choice', async () => {
  api.mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '做一个预约管理工具' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v2/projects/create-workspace', '/api/v2/runs'])
  expect(api.mock.calls[1][1]?.body).toMatchObject({ project_id: 'p1', request: '做一个预约管理工具', operation: 'general', interaction_mode: 'automatic' })
  expect(localStorage.getItem('webuddy:start:goal')).toBeNull()
})

it('keeps the created workspace and identity after a failed submission, then continues', async () => {
  api.mockResolvedValueOnce({ id: 'p1' } as never).mockRejectedValueOnce(new Error('网络中断')).mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '做一个预约管理工具' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('网络中断')
  expect(localStorage.getItem('webuddy:start:goal')).toBe('做一个预约管理工具')
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v2/projects/create-workspace', '/api/v2/runs', '/api/v2/runs'])
  expect(api.mock.calls[1][1]?.body).toEqual(api.mock.calls[2][1]?.body)
})

it('imports attached files before submitting the run', async () => {
  api.mockResolvedValueOnce({ project: { id: 'p2' } } as never).mockResolvedValueOnce({ id: 'r2' } as never)
  show()
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '转换这个样例' } })
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['x'], 'sample.csv')] } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('工作区已打开')
  expect(api.mock.calls[0][0]).toBe('/api/v2/projects/import-files')
  expect((api.mock.calls[0][1]?.body as FormData).getAll('files')).toHaveLength(1)
})
