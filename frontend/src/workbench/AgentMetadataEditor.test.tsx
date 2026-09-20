// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AgentMetadataEditor from './AgentMetadataEditor'
import { request, WorkspaceApiError } from '../workspace/api'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)

const agent = { id: 'a1', name: '报价助手', purpose: '按报价单计算', updated_at: '2026-09-10T08:00:00Z' }
const onSaved = vi.fn()
const onCancel = vi.fn()

beforeEach(() => { api.mockReset(); onSaved.mockReset(); onCancel.mockReset() })
afterEach(cleanup)

function show() {
  render(
    <MemoryRouter>
      <AgentMetadataEditor agent={agent} csrfToken="csrf" onUnauthorized={vi.fn()} onSaved={onSaved} onCancel={onCancel} />
    </MemoryRouter>,
  )
}

it('saves name and purpose via PATCH metadata', async () => {
  const updated = { ...agent, name: '新名字', purpose: '新用途', updated_at: '2026-09-10T09:00:00Z' }
  api.mockResolvedValue(updated as never)
  show()
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新名字' } })
  fireEvent.change(screen.getByLabelText('用途'), { target: { value: '新用途' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(updated))
  expect(api).toHaveBeenCalledWith('/api/v4/agents/a1/metadata', expect.objectContaining({ method: 'PATCH', body: { name: '新名字', purpose: '新用途', expected_updated_at: agent.updated_at } }))
})

it('cancel calls onCancel without saving', () => {
  show()
  fireEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(onCancel).toHaveBeenCalled()
  expect(api).not.toHaveBeenCalled()
})

it('preserves user input on request failure', async () => {
  api.mockImplementation(async () => { throw { detail: '网络错误' } })
  show()
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '输入的名字' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.queryByText('网络错误')).not.toBeNull())
  expect((screen.getByLabelText('名称') as HTMLInputElement).value).toBe('输入的名字')
  expect(onSaved).not.toHaveBeenCalled()
})

it('prevents double submit while saving', async () => {
  let resolveRequest!: (v: unknown) => void
  api.mockImplementation(() => new Promise(r => { resolveRequest = r }))
  show()
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新名字' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.queryByText('保存中…')).not.toBeNull())
  resolveRequest({ ...agent, name: '新名字' })
  await waitFor(() => expect(onSaved).toHaveBeenCalled())
})

it('shows conflict message on 409', async () => {
  api.mockImplementation(async () => { throw new WorkspaceApiError(409, '版本冲突') })
  show()
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新名字' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.queryByText(/刷新后重试/)).not.toBeNull())
})

it('disables save button when name is blank', () => {
  show()
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '   ' } })
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api).not.toHaveBeenCalled()
})
