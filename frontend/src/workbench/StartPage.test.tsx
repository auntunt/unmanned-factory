// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import StartPage from './StartPage'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const show = () => render(<MemoryRouter><Routes><Route path="/" element={<StartPage csrfToken="csrf" onUnauthorized={vi.fn()} />} /><Route path="/runs/:id" element={<div>成果与进度</div>} /></Routes></MemoryRouter>)
beforeEach(() => { localStorage.clear(); api.mockReset() })
afterEach(cleanup)
it('starts from a single goal without fetching assistant or operation choices', async () => {
  api.mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('描述你想要的产品'), { target: { value: '制作订单管理应用' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('成果与进度')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v2/projects/create-workspace', '/api/v2/runs'])
  expect(api.mock.calls[1][1]?.body).toMatchObject({ project_id: 'p1', request: '制作订单管理应用', operation: 'general', interaction_mode: 'automatic' })
  expect(localStorage.getItem('webuddy:start:goal')).toBeNull()
})
it('retains the created workspace and submission identity after a failed run submission', async () => {
  api.mockResolvedValueOnce({ id: 'p1' } as never).mockRejectedValueOnce(new Error('网络中断')).mockResolvedValueOnce({ id: 'r1' } as never)
  show()
  fireEvent.change(screen.getByLabelText('描述你想要的产品'), { target: { value: '制作订单管理应用' } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('网络中断')
  expect(localStorage.getItem('webuddy:start:goal')).toBe('制作订单管理应用')
  fireEvent.click(screen.getByRole('button', { name: '继续制作' }))
  await screen.findByText('成果与进度')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v2/projects/create-workspace', '/api/v2/runs', '/api/v2/runs'])
  expect(api.mock.calls[1][1]?.body).toEqual(api.mock.calls[2][1]?.body)
})
it('imports supplied files before automatic execution', async () => {
  api.mockResolvedValueOnce({ project: { id: 'p2' } } as never).mockResolvedValueOnce({ id: 'r2' } as never)
  show()
  fireEvent.change(screen.getByLabelText('描述你想要的产品'), { target: { value: '转换这个样例' } })
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['sample'], 'sample.csv')] } })
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('成果与进度')
  expect(api.mock.calls[0][0]).toBe('/api/v2/projects/import-files')
  expect((api.mock.calls[0][1]?.body as FormData).getAll('files')).toHaveLength(1)
  expect(api.mock.calls[1][1]?.body).toMatchObject({ project_id: 'p2', interaction_mode: 'automatic' })
})
