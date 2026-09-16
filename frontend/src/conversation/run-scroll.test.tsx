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
const run = { id: 'r1', project_id: 'p1', request: '做工具', revision: 1, plan: { title: '工具', summary: '', questions: [], tasks: [] }, artifacts: {}, status: 'running', created_at: '', updated_at: '' }
const messages = [{ id: 1, role: 'user', content: '第一条消息', at: '' }, { id: 2, role: 'assistant', content: '最新一条消息', at: '' }]

function mountInShell() {
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v2/runs/r1' && !opts?.method) return run as never
    if (url === '/api/v2/runs/r1/conversation') return { messages } as never
    return {} as never
  })
  const view = render(<div className="as-content"><MemoryRouter initialEntries={['/runs/r1']}>
    <WorkTitleContext.Provider value={vi.fn()}>
      <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="x" onUnauthorized={noop} pollMs={0} />} /></Routes>
    </WorkTitleContext.Provider>
  </MemoryRouter></div>)
  const scroller = view.container.querySelector('.as-content') as HTMLElement
  Object.defineProperty(scroller, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(scroller, 'clientHeight', { value: 300, configurable: true })
  scroller.scrollTop = 0
  return scroller
}
beforeEach(() => api.mockReset())
afterEach(cleanup)

it('follows new messages by scrolling the shell content container, not the visible-overflow wrapper', async () => {
  const scroller = mountInShell()
  await screen.findByText('最新一条消息')
  await waitFor(() => expect(scroller.scrollTop).toBe(1000))
})

it('stops following once the reader scrolls up to read history', async () => {
  const scroller = mountInShell()
  await screen.findByText('最新一条消息')
  await waitFor(() => expect(scroller.scrollTop).toBe(1000))
  // Reader scrolls up; the scroll handler clears the stick-to-bottom intent.
  scroller.scrollTop = 100
  fireEvent.scroll(scroller)
  // A later status refresh must not yank them back to the bottom.
  scroller.scrollTop = 100
  Object.defineProperty(scroller, 'scrollHeight', { value: 2000, configurable: true })
  fireEvent.scroll(scroller)
  expect(scroller.scrollTop).toBe(100)
})
