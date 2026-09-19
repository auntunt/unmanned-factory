// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import PackBind from './PackBind'
import { request } from '../workspace/api'
vi.mock('../workspace/api', () => ({ request: vi.fn() }))
afterEach(cleanup)
it('binds the published version with the actual current binding revision', async () => {
  const api = vi.mocked(request)
  api.mockImplementation(async (url, options) => {
    if (url === '/api/v4/agents') return { agents: [{ id: 'a1', name: '会议助手' }] }
    if (!options?.method) return { bindings: [{ pack_id: 'p1', revision: 3 }] }
    return { version: 2 }
  })
  const changed = vi.fn()
  render(<MemoryRouter><PackBind packId="p1" versionId="v2" version={2} csrfToken="csrf" onUnauthorized={() => {}} onChanged={changed} /></MemoryRouter>)
  await screen.findByRole('option', { name: '会议助手' })
  fireEvent.change(screen.getByLabelText('目标职能体'), { target: { value: 'a1' } })
  fireEvent.click(screen.getByRole('button', { name: '挂靠 v2' }))
  expect((await screen.findByRole('link', { name: '打开职能体使用工具' })).getAttribute('href')).toBe('/agents/a1/chat')
  expect(api).toHaveBeenCalledWith('/api/v4/capability-packs/bindings', expect.objectContaining({ body: { agent_id: 'a1', version_id: 'v2', expected_revision: 3 } }))
  expect(changed).toHaveBeenCalledOnce()
})
