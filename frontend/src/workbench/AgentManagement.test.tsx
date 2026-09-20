// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import AgentsPage from './AgentsPage'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
vi.mock('./AgentManifest', () => ({ default: () => <div>岗位清单区域</div> }))
vi.mock('./AgentEvolution', () => ({ default: () => null }))

const api = vi.mocked(request)
const agent = { id: 'a1', name: '报价助手', purpose: '计算报价', active_version: 1, version: { version: 1 }, updated_at: '2026-09-10T00:00:00Z' }
const adminProps = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }

beforeEach(() => {
  api.mockReset()
  api.mockImplementation(async (path: string) => {
    if (path === '/api/v4/agents') return { agents: [agent] } as never
    if (path === '/api/v4/agents/a1') return agent as never
    if (String(path).includes('/preflight')) return { ready: true, message: '' } as never
    if (String(path).includes('/bindings/')) return { bindings: [] } as never
    if (String(path).includes('/modules')) return { modules: [] } as never
    if (String(path).includes('/capabilities')) return { capabilities: [] } as never
    return { items: [], skills: [], proposals: [], conversations: [], versions: [] } as never
  })
})
afterEach(cleanup)

function showDetail() {
  render(
    <MemoryRouter initialEntries={['/agents/a1']}>
      <Routes>
        <Route path="/agents/:agentId" element={<AgentsPage {...adminProps} />} />
      </Routes>
    </MemoryRouter>,
  )
}

it('management page shows three sections: basic info, work specs, attached tools', async () => {
  showDetail()
  await screen.findByRole('region', { name: '基本信息' })
  expect(screen.getByRole('region', { name: '工作规范' })).toBeTruthy()
  expect(screen.getByRole('region', { name: '已挂靠工具' })).toBeTruthy()
})

it('management page does not show left sidebar agent list', async () => {
  showDetail()
  await screen.findByRole('region', { name: '基本信息' })
  // The old "你的职能体" sidebar should not exist
  expect(screen.queryByText('你的职能体')).toBeNull()
  expect(screen.queryByRole('complementary', { name: '职能体与最近任务' })).toBeNull()
})

it('three add-ability sources show one at a time', async () => {
  showDetail()
  await screen.findByRole('region', { name: '添加能力' })

  // Initially no source form is shown
  expect(screen.queryByTestId('source-upload')).toBeNull()
  expect(screen.queryByTestId('source-team')).toBeNull()
  expect(screen.queryByTestId('source-dev')).toBeNull()

  // Click "上传包" -- only upload form appears
  fireEvent.click(screen.getByRole('button', { name: '上传包' }))
  expect(screen.getByTestId('source-upload')).toBeTruthy()
  expect(screen.queryByTestId('source-team')).toBeNull()
  expect(screen.queryByTestId('source-dev')).toBeNull()

  // Switch to "团队已有能力" -- upload disappears, team appears
  fireEvent.click(screen.getByRole('button', { name: '团队已有能力' }))
  expect(screen.queryByTestId('source-upload')).toBeNull()
  expect(screen.getByTestId('source-team')).toBeTruthy()
  expect(screen.queryByTestId('source-dev')).toBeNull()

  // Switch to "开发成果" -- team disappears, dev appears
  fireEvent.click(screen.getByRole('button', { name: '开发成果' }))
  expect(screen.queryByTestId('source-upload')).toBeNull()
  expect(screen.queryByTestId('source-team')).toBeNull()
  expect(screen.getByTestId('source-dev')).toBeTruthy()

  // Click same button again to deselect -- nothing shown
  fireEvent.click(screen.getByRole('button', { name: '开发成果' }))
  expect(screen.queryByTestId('source-upload')).toBeNull()
  expect(screen.queryByTestId('source-team')).toBeNull()
  expect(screen.queryByTestId('source-dev')).toBeNull()
})

it('basic info section shows edit button for admin and can open editor', async () => {
  showDetail()
  await screen.findByText('报价助手')
  const editBtn = screen.getByRole('button', { name: '编辑名称与用途' })
  expect(editBtn).toBeTruthy()

  fireEvent.click(editBtn)
  expect(screen.getByTestId('metadata-editor')).toBeTruthy()
})

it('upload source clearly distinguishes Skill ZIP from agent pack import', async () => {
  showDetail()
  await screen.findByRole('region', { name: '添加能力' })
  fireEvent.click(screen.getByRole('button', { name: '上传包' }))
  const form = screen.getByTestId('source-upload')
  // The user must see that importing a full agent pack goes through a different entry point.
  expect(form.textContent).toContain('导入职能体')
})

it('?mode=maintain deep link still reaches the management page', async () => {
  render(
    <MemoryRouter initialEntries={['/agents/a1?mode=maintain']}>
      <Routes>
        <Route path="/agents/:agentId" element={<AgentsPage {...adminProps} />} />
      </Routes>
    </MemoryRouter>,
  )
  // The page should still render the management sections
  await screen.findByRole('region', { name: '基本信息' })
  expect(screen.getByRole('region', { name: '工作规范' })).toBeTruthy()
})
