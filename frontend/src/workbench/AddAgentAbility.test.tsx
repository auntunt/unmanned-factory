// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { request } from '../workspace/api'
import AddAgentAbility from './AddAgentAbility'
import AgentsPage from './AgentsPage'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
vi.mock('./AgentManifest', () => ({ default: () => <div>岗位清单测试</div> }))
vi.mock('./AgentEvolution', () => ({ default: () => null }))
const api = vi.mocked(request)
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }
beforeEach(() => { api.mockReset() })
afterEach(cleanup)
function show() { render(<MemoryRouter><AddAgentAbility agentId="a1" onChanged={vi.fn()} {...props} /></MemoryRouter>) }
it('adds prompt to the existing maintenance pipeline without a project', async () => {
  api.mockImplementation(async (path) => path.endsWith('/conversations') ? { id: 'c1' } : { items: [] })
  show(); fireEvent.click(screen.getByText('其他资料来源')); fireEvent.click(screen.getByRole('button', { name: '粘贴 Prompt' })); fireEvent.change(screen.getByLabelText('Prompt 素材'), { target: { value: '以后先核对兼容性' } })
  fireEvent.click(screen.getByRole('button', { name: '交给维护会话整理' }))
  await screen.findByText(/Prompt 已作为维护会话素材保存/)
  expect(api).toHaveBeenCalledWith('/api/v4/agents/a1/conversations', expect.objectContaining({ body: { mode: 'maintain', project_id: null } }))
  expect(api).toHaveBeenCalledWith('/api/v4/conversations/c1/messages', expect.objectContaining({ body: { content: '以后先核对兼容性' } }))
  expect(screen.queryByLabelText('所属项目')).toBeNull()
})
it.each(['attachment', 'adaptation'])('shows server classification %s with no project selector', async channel => {
  api.mockImplementation(async (_path, options) => options?.method === 'POST' ? { channel, message: channel === 'attachment' ? '单 skill，直接添加' : '检测到 90 个 skill 的外部包，将走适配与人签', ingestion: channel === 'adaptation' ? { run_id: 'r1' } : undefined } : { items: [] })
  show()
  const file = new File(['zip'], 'reverse-skill.zip')
  fireEvent.change(screen.getByLabelText('能力 ZIP 文件'), { target: { files: [file] } })
  await screen.findByText(channel === 'attachment' ? /单 skill，直接添加/ : /检测到 90 个 skill/)
  const sent = api.mock.calls.find(([path]) => path.endsWith('/abilities'))![1]?.body as FormData
  expect(sent.get('file')).toBe(file); expect(sent.get('project_id')).toBeNull()
  if (channel === 'adaptation') expect(screen.getByRole('link', { name: '查看适配运行' }).getAttribute('href')).toBe('/runs/r1')
})
it('shows a specific server rejection', async () => {
  api.mockImplementation(async (_path, options) => { if (options?.method === 'POST') throw new Error('单文件超过 2 MiB'); return { items: [] } })
  show()
  fireEvent.change(screen.getByLabelText('能力 ZIP 文件'), { target: { files: [new File(['zip'], 'bad.zip')] } })
  await screen.findByText('单文件超过 2 MiB')
})
it('reads a mounted directory in agent scope', async () => {
  api.mockImplementation(async (_path, options) => options?.method === 'POST' ? { run_id: 'r1' } : { items: [] })
  show(); fireEvent.click(screen.getByText('其他资料来源')); fireEvent.click(screen.getByRole('button', { name: '读取目录' }))
  fireEvent.change(screen.getByLabelText('资料相对目录'), { target: { value: 'skills/library' } })
  fireEvent.click(screen.getByRole('button', { name: '读取并适配' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/skill-ingestions/directory', expect.objectContaining({ body: { agent_id: 'a1', path: 'skills/library' } })))
})
it.each([false, true])('native import belongs only to the list (detail=%s)', async detail => {
  const agent = { id: 'a1', name: '维护员', active_version: 1, version: { version: 1 } }
  api.mockImplementation(async path => path === '/api/v4/agents' ? { agents: [agent] } : path === '/api/v4/agents/a1' ? agent : path.endsWith('/draft') ? { agent_id: 'a1', revision: 0, patch: {} } : { items: [], projects: [], conversations: [], versions: [] })
  render(<MemoryRouter initialEntries={[detail ? '/agents/a1' : '/agents']}><Routes><Route path="/agents/:agentId?" element={<AgentsPage {...props} />} /></Routes></MemoryRouter>)
  if (detail) {
    await screen.findByRole('button', { name: '维护职能体' })
    expect(screen.queryByRole('region', { name: '为职能体添加能力' })).toBeNull()
    expect(screen.queryByText('模型与工具')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '维护职能体' }))
    await screen.findByRole('region', { name: '为职能体添加能力' })
    expect(screen.queryByRole('button', { name: '导入职能体' })).toBeNull()
    expect(screen.queryByLabelText('选择 webuddy 职能包 ZIP')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '维护职能体' }))
    expect(screen.queryByText('添加 Skill（ZIP）')).toBeNull()
  } else {
    fireEvent.click(screen.getByRole('button', { name: '导入职能体' }))
    expect(screen.getByLabelText('选择 webuddy 职能包 ZIP')).toBeTruthy()
    expect(screen.queryByRole('region', { name: '为职能体添加能力' })).toBeNull()
  }
})

it('shows configuration blockers before uploading a package', async () => {
  api.mockImplementation(async path => path.endsWith('/preflight')
    ? { ready: false, message: '请管理员配置零工具 Claude' } : { items: [] })
  show()
  await screen.findByText('请管理员配置零工具 Claude')
  expect((screen.getByLabelText('能力 ZIP 文件') as HTMLInputElement).disabled).toBe(true)
  expect(api.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
})
