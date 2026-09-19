// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PackFromDeliverables from './PackFromDeliverables'
import { request } from '../workspace/api'
vi.mock('../workspace/api', () => ({ request: vi.fn() }))
const api = vi.mocked(request)
beforeEach(() => api.mockReset())
afterEach(cleanup)

it('creates only a draft from explicitly selected files and declared sample scope', async () => {
  api.mockResolvedValue({ id: 'p1' })
  render(<MemoryRouter><PackFromDeliverables runId="r1" csrfToken="csrf" onUnauthorized={() => {}}
    files={[{ name: 'webuddy-pack.json', size: 10 }, { name: 'fixtures/sample.json', size: 20 }, { name: 'input/client.txt', size: 30 }]} /></MemoryRouter>)
  fireEvent.click(screen.getByText('转换为可复用工具'))
  expect((screen.getByRole('button', { name: '创建工具包草稿' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByLabelText('webuddy-pack.json', { selector: 'input' }))
  fireEvent.click(screen.getByLabelText('fixtures/sample.json', { selector: 'input' }))
  fireEvent.change(screen.getByLabelText('fixtures/sample.json 文件用途'), { target: { value: 'synthetic' } })
  fireEvent.click(screen.getByRole('button', { name: '创建工具包草稿' }))
  expect((await screen.findByRole('link', { name: '查看草稿并运行验证' })).getAttribute('href')).toBe('/ability-center/packs/p1')
  expect(api).toHaveBeenCalledTimes(1)
  expect(api.mock.calls[0][1]).toMatchObject({ body: { source_run_id: 'r1', selections: [
    { name: 'webuddy-pack.json' }, { name: 'fixtures/sample.json', material_scope: 'synthetic' },
  ] } })
})

it('keeps the same operation key after a lost response and shows the server error', async () => {
  api.mockRejectedValueOnce(new Error('连接中断')).mockResolvedValueOnce({ id: 'p2' })
  render(<MemoryRouter><PackFromDeliverables runId="r2" csrfToken="csrf" onUnauthorized={() => {}}
    files={[{ name: 'tool/main.py', size: 10 }]} /></MemoryRouter>)
  fireEvent.click(screen.getByText('转换为可复用工具'))
  fireEvent.click(screen.getByLabelText('tool/main.py', { selector: 'input' }))
  fireEvent.click(screen.getByRole('button', { name: '创建工具包草稿' }))
  expect((await screen.findByRole('alert')).textContent).toContain('连接中断')
  fireEvent.click(screen.getByRole('button', { name: '创建工具包草稿' }))
  await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
  expect(api.mock.calls[0][1]?.body).toEqual(api.mock.calls[1][1]?.body)
})
