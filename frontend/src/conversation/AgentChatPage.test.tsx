// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import AgentChatPage from './AgentChatPage'
import { WorkTitleContext } from './title-context'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()
const agent = { id: 'a1', name: '报价助手', purpose: '按报价单计算与解释', active_version: 1 }

function mount() {
  return render(<MemoryRouter initialEntries={['/agents/a1/chat']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/agents/:agentId/chat" element={<AgentChatPage csrfToken="x" onUnauthorized={noop} user={{ id: 1, username: 'owner', role: 'admin' }} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
}
beforeEach(() => api.mockReset())
afterEach(cleanup)

it('opens a no-project do conversation and sends a message without creating a project or run', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url === '/api/v4/agents/a1' && !opts?.method) return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: [] } as never
    if (url === '/api/v4/agents/a1/conversations' && opts?.method === 'POST') { expect((opts.body as { project_id?: unknown }).project_id).toBeNull(); return { id: 'c1', agent_id: 'a1', mode: 'do', project_id: null, messages: [] } as never }
    if (url === '/api/v4/conversations/c1/messages' && opts?.method === 'POST') return { conversation: { id: 'c1', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'm1', role: 'user', content: '这单报价多少' }], run_id: null }, run: null } as never
    return {} as never
  })
  mount()
  await screen.findByText('报价助手')
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '这单报价多少' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await screen.findByText('这单报价多少')
  const calls = api.mock.calls.map(([u, o]) => `${(o as { method?: string })?.method || 'GET'} ${u}`)
  expect(calls).toContain('POST /api/v4/agents/a1/conversations')
  expect(calls).toContain('POST /api/v4/conversations/c1/messages')
  // No project / dev-run endpoints touched.
  expect(calls.some(c => c.includes('/api/v2/projects') || c.includes('/api/v2/runs'))).toBe(false)
})

it('recovers the latest conversation on load (refresh), showing prior turns', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations') return { conversations: [{ id: 'c9', agent_id: 'a1', mode: 'do', messages: [{ id: 'u', role: 'user', content: '上次的问题' }] }] } as never
    if (url === '/api/v4/conversations/c9') return { id: 'c9', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'u', role: 'user', content: '上次的问题' }, { id: 'a', role: 'assistant', content: '上次的回答', status: 'completed' }] } as never
    return {} as never
  })
  mount()
  expect(await screen.findByText('上次的问题')).toBeTruthy()
  expect(screen.getByText('上次的回答')).toBeTruthy()
})

it('opens the exact conversation named in ?cid=, not the latest, for refresh and multi-tab', async () => {
  const rows = [{ id: 'c-latest', agent_id: 'a1', mode: 'do', messages: [] }, { id: 'c-target', agent_id: 'a1', mode: 'do', messages: [] }]
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v4/agents/a1' && !opts?.method) return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: rows } as never
    if (url === '/api/v4/conversations/c-target' && !opts?.method) return { id: 'c-target', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'm1', role: 'user', content: '指定会话内容' }], run_id: null } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/agents/a1/chat?cid=c-target']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/agents/:agentId/chat" element={<AgentChatPage csrfToken="x" onUnauthorized={noop} user={{ id: 1, username: 'owner', role: 'admin' }} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('指定会话内容')
  const gets = api.mock.calls.filter(([, o]) => !(o as { method?: string })?.method).map(([u]) => u)
  expect(gets).toContain('/api/v4/conversations/c-target')
  expect(gets).not.toContain('/api/v4/conversations/c-latest') // did not fall back to the latest
})
