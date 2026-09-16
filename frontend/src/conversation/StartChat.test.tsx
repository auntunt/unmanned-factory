// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import StartChat from './StartChat'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
type User = { id: number; username: string; role: 'admin' | 'member' }
const show = (user?: User) => render(<MemoryRouter><Routes>
  <Route path="/" element={<StartChat csrfToken="csrf" onUnauthorized={vi.fn()} user={user} />} />
  <Route path="/runs/:id" element={<div>工作区已打开</div>} />
  <Route path="/agents/:id/chat" element={<div>对话已打开</div>} />
</Routes></MemoryRouter>)
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); api.mockReset() })
afterEach(cleanup)
const type = (value: string) => fireEvent.change(screen.getByLabelText('需求'), { target: { value } })
const go = () => fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
const body = (i: number) => api.mock.calls[i][1]?.body as Record<string, unknown>

it('routes a development request through the server, then creates project + run', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show(); type('做一个预约管理工具'); go()
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v2/projects/create-workspace', '/api/v2/runs'])
  expect(body(0)).toMatchObject({ text: '做一个预约管理工具', attachments: [] })
  expect(body(1).idempotency_key).toBe(body(2).idempotency_key) // one op key drives project + run
})

it('routes a daily task to a role chat and navigates to that exact conversation, no run', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('对话已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v4/agents/a9/conversations', '/api/v4/conversations/c1/messages'])
  expect(body(1)).toMatchObject({ mode: 'do', project_id: null })
  expect(body(1).client_key).toBe(body(2).idempotency_key) // conversation + message share the op key
})

it('sends UTF-8 text attachments into the role chat by content, without a project', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9', attach: true } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({} as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('按这个价格表报价')
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['a,b'], 'prices.csv')] } })
  go()
  await screen.findByText('对话已打开')
  expect(api.mock.calls.map(([url]) => url)).toEqual(['/api/v4/route', '/api/v4/agents/a9/conversations', '/api/v4/conversations/c1/attachments', '/api/v4/conversations/c1/messages'])
  expect((body(0).attachments as unknown[])).toHaveLength(1)
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
  expect(sessionStorage.getItem('webuddy:start:draft:anon')).toBe('把这个 mfd 转成 xml')
})

it('on clarify, shows the question and lets the user pick a role', async () => {
  api.mockResolvedValueOnce({ kind: 'clarify', question: '要开发还是让助手处理？', roles: [{ agent_id: 'a9', name: '报价助手' }], offer_development: true } as never)
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('做个报价'); go()
  await screen.findByText('要开发还是让助手处理？')
  fireEvent.click(screen.getByRole('button', { name: '交给「报价助手」' }))
  await screen.findByText('对话已打开')
  expect(body(1)).toMatchObject({ agent_id: 'a9' })
})

it('a lost message response does not create a second conversation on retry', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockRejectedValueOnce(new Error('响应丢失'))
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('响应丢失')
  go()
  await screen.findByText('对话已打开')
  const urls = api.mock.calls.map(([url]) => url)
  expect(urls.filter(u => u === '/api/v4/agents/a9/conversations')).toHaveLength(1) // reused, no duplicate
  const keys = api.mock.calls.filter(([u]) => u === '/api/v4/conversations/c1/messages').map(([, o]) => (o?.body as { idempotency_key: string }).idempotency_key)
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBe(keys[1])
})

it('a lost conversation-create response retries with the SAME client_key so the server dedups', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockRejectedValueOnce(new Error('创建响应丢失'))
    .mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('创建响应丢失')
  go()
  await screen.findByText('对话已打开')
  const creates = api.mock.calls.filter(([u]) => u === '/api/v4/agents/a9/conversations')
  expect(creates).toHaveLength(2)
  expect((creates[0][1]?.body as { client_key: string }).client_key).toBe((creates[1][1]?.body as { client_key: string }).client_key)
})

it('blocks submit when a partly-uploaded material was lost on refresh, instead of sending without it', async () => {
  // Simulate: an earlier attempt created c1 and recorded a material, then a refresh
  // dropped the File. sessionStorage keeps the op; localStorage keeps the goal.
  sessionStorage.setItem('webuddy:start:op:anon', JSON.stringify({ op: 'OP1', kind: 'chat', goal: '按材料报价', agentId: 'a9', cid: 'c1', manifest: [{ name: 'prices.csv', hash: 'deadbeef', size: 10 }], uploaded: [] }))
  sessionStorage.setItem('webuddy:start:draft:anon', '按材料报价')
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9', attach: true } as never)
  show(); go()
  await screen.findByText(/材料未恢复，请重新选择/)
  expect(screen.getByText(/prices\.csv/)).toBeTruthy()
  expect(api.mock.calls.some(([u]) => String(u).includes('/messages'))).toBe(false) // text not sent without material
})

it('does not inherit another account\'s in-flight project (per-actor state)', async () => {
  sessionStorage.setItem('webuddy:start:op:1', JSON.stringify({ op: 'OP-A', kind: 'dev', goal: '做一个管理系统', projectId: 'p-other' }))
  sessionStorage.setItem('webuddy:start:draft:2', '做一个管理系统')
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p-new' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  show({ id: 2, username: 'other', role: 'admin' }); go()
  await screen.findByText('工作区已打开')
  expect(api.mock.calls.map(([url]) => url)).toContain('/api/v2/projects/create-workspace') // fresh workspace
  expect(body(2)).toMatchObject({ project_id: 'p-new' }) // not p-other
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

it('uploads two same-named files with different content as distinct materials (by content)', async () => {
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9', attach: true } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never)
    .mockResolvedValueOnce({} as never).mockResolvedValueOnce({} as never)
    .mockResolvedValueOnce({ job_id: 'j1' } as never)
  show(); type('按这两版价格表报价')
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [new File(['v,1'], 'prices.csv'), new File(['v,2'], 'prices.csv')] } })
  go()
  await screen.findByText('对话已打开')
  const uploads = api.mock.calls.filter(([u]) => u === '/api/v4/conversations/c1/attachments')
  expect(uploads).toHaveLength(2) // same name, different content -> both sent, none skipped
})

const remount = () => { cleanup(); return show() } // simulate a browser refresh (sessionStorage survives)

it('after a lost conversation-create response, a refresh reuses the same client_key', async () => {
  // Merged from the Codex review counter-example: persist BEFORE the first request.
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockRejectedValueOnce(new Error('创建响应丢失'))
  show(); type('帮客户算一批设备的报价'); go()
  await screen.findByText('创建响应丢失')
  const first = (api.mock.calls.find(([u]) => u === '/api/v4/agents/a9/conversations')![1]!.body as { client_key: string }).client_key
  api.mockReset()
  api.mockResolvedValueOnce({ kind: 'agent_chat', agent_id: 'a9' } as never)
    .mockResolvedValueOnce({ id: 'c1' } as never).mockResolvedValueOnce({ job_id: 'j1' } as never)
  remount(); go() // goal restored from the per-actor draft; no re-type
  await screen.findByText('对话已打开')
  const create = api.mock.calls.find(([u]) => u === '/api/v4/agents/a9/conversations')!
  expect((create[1]!.body as { client_key: string }).client_key).toBe(first)
})

it('after a lost project-create response, a refresh reuses the same idempotency key', async () => {
  api.mockResolvedValueOnce({ kind: 'development' } as never).mockRejectedValueOnce(new Error('创建响应丢失'))
  show(); type('做一个预约管理工具'); go()
  await screen.findByText('创建响应丢失')
  const first = (api.mock.calls.find(([u]) => u === '/api/v2/projects/create-workspace')![1]!.body as { idempotency_key: string }).idempotency_key
  api.mockReset()
  api.mockResolvedValueOnce({ kind: 'development' } as never)
    .mockResolvedValueOnce({ id: 'p1' } as never).mockResolvedValueOnce({ id: 'r1' } as never)
  remount(); go()
  await screen.findByText('工作区已打开')
  const create = api.mock.calls.find(([u]) => u === '/api/v2/projects/create-workspace')!
  expect((create[1]!.body as { idempotency_key: string }).idempotency_key).toBe(first)
})

it('a blank/whitespace goal restored on refresh neither submits nor crashes', async () => {
  sessionStorage.setItem('webuddy:start:draft:anon', '   ')
  show()
  expect((screen.getByRole('button', { name: '开始制作' }) as HTMLButtonElement).disabled).toBe(true)
  go()
  expect(api.mock.calls).toHaveLength(0)
})

it('re-selecting the same material after a refresh reuses the conversation, no new op', async () => {
  let convCreates = 0, attachCalls = 0
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/route') return { kind: 'agent_chat', agent_id: 'a9', attach: true } as never
    if (url === '/api/v4/agents/a9/conversations') { convCreates++; return { id: 'c1' } as never }
    if (url === '/api/v4/conversations/c1/attachments') { attachCalls++; if (attachCalls === 1) throw new Error('上传响应丢失'); return {} as never }
    if (url === '/api/v4/conversations/c1/messages') return { job_id: 'j1' } as never
    return {} as never
  })
  const file = () => new File(['a,b,c'], 'prices.csv')
  show(); type('按这个价格表报价')
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [file()] } })
  go()
  await screen.findByText('上传响应丢失')
  remount(); go() // File lost on refresh -> blocked, asks to re-select
  await screen.findByText(/材料未恢复/)
  fireEvent.change(screen.getByLabelText(/添加材料/), { target: { files: [file()] } }) // same content
  go()
  await screen.findByText('对话已打开')
  expect(convCreates).toBe(1) // conversation reused across refresh, not recreated
  expect(attachCalls).toBe(2) // upload retried once, not a fresh op
})

it('one account does not see or reuse another account\'s draft text', async () => {
  sessionStorage.setItem('webuddy:start:draft:1', '账号1的机密草稿')
  show({ id: 2, username: 'other', role: 'admin' })
  expect((screen.getByLabelText('需求') as HTMLTextAreaElement).value).toBe('') // uid 2 sees empty, not uid 1's draft
})

const fileInput = () => screen.getByLabelText(/添加材料/)

it('after a lost import response, a refresh resumes the import and never creates an empty workspace', async () => {
  // Merged from the Codex review counter-example: dev material state is saved.
  api.mockResolvedValueOnce({ kind: 'development' } as never).mockRejectedValueOnce(new Error('导入响应丢失'))
  show(); type('用这些材料做一个管理系统')
  fireEvent.change(fileInput(), { target: { files: [new File(['important'], 'sample.csv')] } })
  go(); await screen.findByText('导入响应丢失')
  api.mockReset(); api.mockResolvedValue({} as never)
  remount(); go()
  await screen.findByText(/材料未恢复/)
  expect(api.mock.calls.some(([u]) => u === '/api/v2/projects/create-workspace')).toBe(false) // no empty project
  expect(api.mock.calls.some(([u]) => u === '/api/v4/route')).toBe(false) // resumed the task, not re-routed
})

it('re-selecting the same material after a refresh imports it and reuses the op, never create-workspace', async () => {
  let imports = 0, creates = 0
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/route') return { kind: 'development' } as never
    if (url === '/api/v2/projects/import-files') { imports++; if (imports === 1) throw new Error('导入响应丢失'); return { project: { id: 'p1' } } as never }
    if (url === '/api/v2/projects/create-workspace') { creates++; return { id: 'empty' } as never }
    if (url === '/api/v2/runs') return { id: 'r1' } as never
    return {} as never
  })
  const material = () => new File(['important'], 'sample.csv')
  show(); type('用这些材料做一个管理系统')
  fireEvent.change(fileInput(), { target: { files: [material()] } }); go()
  await screen.findByText('导入响应丢失')
  remount(); go(); await screen.findByText(/材料未恢复/)
  fireEvent.change(fileInput(), { target: { files: [material()] } }); go()
  await screen.findByText('工作区已打开')
  expect(imports).toBe(2) // retried the SAME import endpoint
  expect(creates).toBe(0) // never fell back to an empty workspace
})

it('explicitly removing all attachments sends a new operation without the removed material', async () => {
  let convs = 0, attaches = 0
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v4/route') return { kind: 'agent_chat', agent_id: 'a9', attach: true } as never
    if (url === '/api/v4/agents/a9/conversations') { convs++; return { id: `c${convs}` } as never }
    if (String(url).endsWith('/attachments')) { attaches++; throw new Error('上传失败') }
    if (String(url).endsWith('/messages')) return { job_id: 'j1' } as never
    return {} as never
  })
  show(); type('按这个材料报价')
  fireEvent.change(fileInput(), { target: { files: [new File(['x'], 'p.csv')] } }); go()
  await screen.findByText('上传失败') // c1 created, upload failed (material recorded)
  fireEvent.click(screen.getByRole('button', { name: /移除 p.csv/ })) // explicit removal
  go()
  await screen.findByText('对话已打开')
  expect(attaches).toBe(1) // the removed material is NOT re-sent
  expect(convs).toBe(2) // explicit removal is a new operation/conversation
})
