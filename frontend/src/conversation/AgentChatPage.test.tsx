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

it('rejects a ?cid that belongs to another role or is not a do chat, without falling back', async () => {
  const rows = [{ id: 'c-mine', agent_id: 'a1', mode: 'do', messages: [] }]
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v4/agents/a1' && !opts?.method) return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: rows } as never
    // The pinned cid resolves to a conversation owned by a DIFFERENT agent.
    if (url === '/api/v4/conversations/c-foreign' && !opts?.method) return { id: 'c-foreign', agent_id: 'other', mode: 'do', project_id: null, messages: [{ id: 'x', role: 'user', content: '别的角色的会话' }] } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/agents/a1/chat?cid=c-foreign']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/agents/:agentId/chat" element={<AgentChatPage csrfToken="x" onUnauthorized={noop} user={{ id: 1, username: 'owner', role: 'admin' }} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  expect(await screen.findByText(/不属于当前助手/)).toBeTruthy()
  expect(screen.queryByText('别的角色的会话')).toBeNull() // did not render the foreign conversation
})

import { useLocation, useNavigate } from 'react-router-dom'
function Probe() {
  const loc = useLocation(); const nav = useNavigate()
  return <div><span data-testid="url">{loc.search}</span><button onClick={() => nav(-1)}>返回</button></div>
}
const twoConvs = [
  { id: 'A', agent_id: 'a1', mode: 'do', messages: [{ id: 'ua', role: 'user', content: '会话A' }] },
  { id: 'B', agent_id: 'a1', mode: 'do', messages: [{ id: 'ub', role: 'user', content: '会话B' }] },
]
function mountAt(entry: string) {
  return render(<MemoryRouter initialEntries={[entry]}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/agents/:agentId/chat" element={<><AgentChatPage csrfToken="x" onUnauthorized={noop} user={{ id: 1, username: 'owner', role: 'admin' }} /><Probe /></>} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
}

it('a late response for the previous conversation does not overwrite the newly selected one', async () => {
  let resolveA: (v: unknown) => void = () => {}
  const aPending = new Promise(r => { resolveA = r })
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations') return { conversations: twoConvs } as never
    if (url === '/api/v4/conversations/A') return aPending as never // slow, resolves after we switch away
    if (url === '/api/v4/conversations/B') return { id: 'B', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'mb', role: 'assistant', content: 'B的回答', status: 'completed' }] } as never
    return {} as never
  })
  mountAt('/agents/a1/chat?cid=A')
  fireEvent.click(await screen.findByRole('button', { name: '会话B' }))
  await screen.findByText('B的回答')
  // The stale A response now arrives; it must be dropped, not rendered over B.
  resolveA({ id: 'A', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'ma', role: 'assistant', content: 'A的迟到回答', status: 'completed' }] })
  await new Promise(r => setTimeout(r, 30))
  expect(screen.getByText('B的回答')).toBeTruthy()
  expect(screen.queryByText('A的迟到回答')).toBeNull()
})

it('history, new-chat and Back keep the URL and shown conversation in sync', async () => {
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations') return { conversations: twoConvs } as never
    if (url === '/api/v4/conversations/A') return { id: 'A', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'ma', role: 'assistant', content: 'A的回答', status: 'completed' }] } as never
    if (url === '/api/v4/conversations/B') return { id: 'B', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'mb', role: 'assistant', content: 'B的回答', status: 'completed' }] } as never
    return {} as never
  })
  mountAt('/agents/a1/chat?cid=A')
  await screen.findByText('A的回答')
  expect(screen.getByTestId('url').textContent).toBe('?cid=A')
  fireEvent.click(screen.getByRole('button', { name: '会话B' }))
  await screen.findByText('B的回答')
  expect(screen.getByTestId('url').textContent).toBe('?cid=B')
  fireEvent.click(screen.getByRole('button', { name: /新对话/ }))
  await screen.findByText(/聊点什么/) // empty state for a fresh conversation
  expect(screen.getByTestId('url').textContent).toBe('?cid=new')
  fireEvent.click(screen.getByRole('button', { name: '返回' })) // Back returns to B
  await screen.findByText('B的回答')
  expect(screen.getByTestId('url').textContent).toBe('?cid=B')
})

it('a stale send result does not navigate back to the previous conversation', async () => {
  let resolveMsg: (v: unknown) => void = () => {}
  const msgPending = new Promise(r => { resolveMsg = r })
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: twoConvs } as never
    if (url === '/api/v4/conversations/A' && !opts?.method) return { id: 'A', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'ua', role: 'user', content: '会话A' }] } as never
    if (url === '/api/v4/conversations/B' && !opts?.method) return { id: 'B', agent_id: 'a1', mode: 'do', project_id: null, messages: [{ id: 'ub', role: 'user', content: '会话B' }] } as never
    if (url === '/api/v4/conversations/A/messages') return msgPending as never
    return {} as never
  })
  mountAt('/agents/a1/chat?cid=A')
  await screen.findByText('会话A')
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '发给A' } })
  fireEvent.click(screen.getByLabelText('发送'))
  fireEvent.click(await screen.findByRole('button', { name: '会话B' }))
  await screen.findByText('会话B')
  expect(screen.getByTestId('url').textContent).toBe('?cid=B')
  resolveMsg({ conversation: { id: 'A', agent_id: 'a1', mode: 'do', messages: [{ id: 'ua', role: 'user', content: '会话A' }] } })
  await new Promise(r => setTimeout(r, 30))
  expect(screen.getByTestId('url').textContent).toBe('?cid=B') // stale send did not jump back to A
  expect(screen.getAllByText('会话B').length).toBeGreaterThan(0)
})

it('while an attachment is uploading on a new conversation, send is blocked (no forked conversation)', async () => {
  let convCreates = 0
  let resolveAttach: (v: unknown) => void = () => {}
  const attachPending = new Promise(r => { resolveAttach = r })
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: [] } as never
    if (url === '/api/v4/agents/a1/conversations' && opts?.method === 'POST') { convCreates++; return { id: 'c1', agent_id: 'a1', mode: 'do', messages: [] } as never }
    if (String(url).endsWith('/attachments')) return attachPending as never
    if (String(url).endsWith('/messages')) return { conversation: { id: 'c1', agent_id: 'a1', mode: 'do', messages: [] } } as never
    return {} as never
  })
  mountAt('/agents/a1/chat?cid=new')
  await screen.findByText(/聊点什么/)
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['x'], 'a.txt')] } })
  await new Promise(r => setTimeout(r, 10)) // attach creates c1, then blocks on upload
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '并行发送' } })
  const sendBtn = screen.getByLabelText('发送') as HTMLButtonElement
  expect(sendBtn.disabled).toBe(true) // send disabled while an upload is in progress
  fireEvent.click(sendBtn)
  resolveAttach({ conversation: { id: 'c1', agent_id: 'a1', mode: 'do', messages: [], attachments: [{ id: 'at1', name: 'a.txt' }] } })
  await new Promise(r => setTimeout(r, 20))
  expect(convCreates).toBe(1) // only the upload created a conversation; send did not fork a second
})

it('a delayed poll for the previous conversation keeps the new one, and the next message goes to it', async () => {
  vi.useFakeTimers()
  let aGets = 0
  let resolvePoll: (v: unknown) => void = () => {}
  api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
    if (url === '/api/v4/agents/a1') return agent as never
    if (url === '/api/v4/agents/a1/conversations' && !opts?.method) return { conversations: twoConvs } as never
    if (url === '/api/v4/conversations/A' && !opts?.method) {
      aGets++
      if (aGets === 1) return { id: 'A', agent_id: 'a1', mode: 'do', messages: [{ id: 'ua', role: 'user', content: '会话A' }, { id: 'aa', role: 'assistant', content: 'A回答中', status: 'running' }] } as never
      return new Promise(r => { resolvePoll = r }) as never // the poll hangs, then arrives late
    }
    if (url === '/api/v4/conversations/B' && !opts?.method) return { id: 'B', agent_id: 'a1', mode: 'do', messages: [{ id: 'ub', role: 'user', content: '会话B' }] } as never
    if (url === '/api/v4/conversations/B/messages' && opts?.method === 'POST') return { conversation: { id: 'B', agent_id: 'a1', mode: 'do', messages: [{ id: 'ub', role: 'user', content: '会话B' }] } } as never
    return {} as never
  })
  mountAt('/agents/a1/chat?cid=A')
  await vi.advanceTimersByTimeAsync(0)
  await vi.advanceTimersByTimeAsync(2000) // poll fires -> A GET #2 hangs
  fireEvent.click(screen.getByRole('button', { name: '会话B' }))
  await vi.advanceTimersByTimeAsync(0)
  resolvePoll({ id: 'A', agent_id: 'a1', mode: 'do', messages: [{ id: 'ua', role: 'user', content: '会话A' }, { id: 'aa', role: 'assistant', content: 'A迟到回答', status: 'completed' }] })
  await vi.advanceTimersByTimeAsync(0)
  expect(screen.queryByText('A迟到回答')).toBeNull() // stale poll dropped
  expect(screen.getAllByText('会话B').length).toBeGreaterThan(0)
  fireEvent.change(screen.getByLabelText('消息'), { target: { value: '发给B' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await vi.advanceTimersByTimeAsync(0)
  expect(api.mock.calls.some(([u, o]) => u === '/api/v4/conversations/B/messages' && (o as { method?: string })?.method === 'POST')).toBe(true)
  expect(api.mock.calls.some(([u, o]) => u === '/api/v4/conversations/A/messages' && (o as { method?: string })?.method === 'POST')).toBe(false)
  vi.useRealTimers()
})
