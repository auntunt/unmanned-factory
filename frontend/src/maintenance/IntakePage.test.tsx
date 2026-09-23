// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import IntakePage from './IntakePage'
import { maintenanceApi } from './api'
import type { RepoView, Requirement } from './types'
import { WorkspaceApiError } from '../workspace/api'

vi.mock('./api', () => ({
  maintenanceApi: {
    repos: vi.fn(),
    submitRequirement: vi.fn(),
    requirements: vi.fn(),
    dispatchRequirement: vi.fn(),
    intakeSources: vi.fn(),
    createIntakeSource: vi.fn(),
    revokeIntakeSource: vi.fn(),
  },
}))
const api = vi.mocked(maintenanceApi)
const noop = vi.fn()
afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => {
  // 管理员区块总会挂载，给出一个安全默认值，测试里按需覆盖。
  api.intakeSources.mockResolvedValue({ sources: [] })
})

const project: RepoView = {
  project_id: 'proj-1', name: '订单服务', repository: 'org/orders', workspace: '/data/orders',
  base_branch: 'main', state: 'ready', state_label: '可开始维护', probe: null, needs: [],
  checks_configured: [], memory: { entries: 0, confirmed: 0 }, credential_ref: null,
}

const requirement: Requirement = {
  requirement_id: 'req-1', project_id: 'proj-1', project_name: '订单服务',
  source: { kind: 'manual', name: '张三' }, external_id: null, title: '', content: '订单导出报错',
  attachments: [], received_at: '2026-09-22T09:00:00Z', status: 'pending_dispatch',
  status_label: '已接收，待派发', task_id: null, task_status: null, dispatch_error: null,
}

function renderPage(role: 'admin' | 'member' = 'admin') {
  return render(
    <MemoryRouter>
      <IntakePage csrfToken="csrf" onUnauthorized={noop} user={{ id: 'u1', username: 'u', role } as never} />
    </MemoryRouter>,
  )
}

describe('需求接入 · 主动提交', () => {
  it('没有项目时显示空状态，不出现提交表单', async () => {
    api.repos.mockResolvedValue({ repos: [] })
    api.requirements.mockResolvedValue({ requirements: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有可提交的项目')).toBeTruthy())
    expect(screen.queryByTestId('manual-intake-form')).toBeNull()
  })

  it('提交需求成功后显示回执，含「已接收 ≠ 已执行」', async () => {
    api.repos.mockResolvedValue({ repos: [project] })
    api.requirements.mockResolvedValue({ requirements: [] })
    api.submitRequirement.mockResolvedValue({
      requirement_id: 'req-9', status: 'pending_dispatch', duplicate: false, task_id: null, execution_id: null,
      clarification: { state: 'not_needed', questions: [] }, received_at: '2026-09-22T09:00:00Z',
      source: { kind: 'manual', name: 'u' }, message: '已接收，不等于已执行',
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('manual-intake-form')).toBeTruthy())

    fireEvent.change(screen.getByLabelText('项目'), { target: { value: 'proj-1' } })
    fireEvent.change(screen.getByLabelText('需要处理什么？'), { target: { value: '订单导出报错' } })
    fireEvent.click(screen.getByText('提交需求'))

    await waitFor(() => expect(screen.getByTestId('manual-receipt')).toBeTruthy())
    expect(screen.getByTestId('manual-receipt').textContent).toContain('已接收 ≠ 已执行')
    expect(screen.getByTestId('manual-receipt').textContent).toContain('已接收，待派发')
    expect(api.submitRequirement).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: 'proj-1', content: '订单导出报错' }),
      { csrfToken: 'csrf', onUnauthorized: noop },
    )
  })
})

describe('需求接入 · 权限', () => {
  it('member 角色看不到接入来源管理', async () => {
    api.repos.mockResolvedValue({ repos: [project] })
    api.requirements.mockResolvedValue({ requirements: [] })
    renderPage('member')
    await waitFor(() => expect(screen.getByTestId('manual-intake-form')).toBeTruthy())
    expect(screen.queryByTestId('intake-sources-admin')).toBeNull()
  })

  it('admin 角色可以创建接入来源，令牌只显示一次', async () => {
    api.repos.mockResolvedValue({ repos: [project] })
    api.requirements.mockResolvedValue({ requirements: [] })
    api.intakeSources.mockResolvedValue({ sources: [] })
    api.createIntakeSource.mockResolvedValue({
      id: 'src-1', name: '告警平台', project_ids: ['proj-1'], auto_dispatch: false,
      created_at: '2026-09-22T09:00:00Z', revoked_at: null, token_hint: 'tok_***abcd', token: 'tok_live_secret',
    })
    renderPage('admin')
    await waitFor(() => expect(screen.getByTestId('intake-sources-admin')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText('例如 告警平台'), { target: { value: '告警平台' } })
    fireEvent.click(screen.getByLabelText('订单服务'))
    fireEvent.click(screen.getByText('创建接入来源'))

    await waitFor(() => expect(screen.getByTestId('new-token-box')).toBeTruthy())
    expect(screen.getByTestId('new-token-box').textContent).toContain('tok_live_secret')
  })
})

describe('需求接入 · 接收记录', () => {
  it('pending_dispatch 的记录显示派发执行按钮，点击调用 dispatchRequirement', async () => {
    api.repos.mockResolvedValue({ repos: [project] })
    api.requirements.mockResolvedValue({ requirements: [requirement] })
    api.dispatchRequirement.mockResolvedValue({
      requirement_id: 'req-1', status: 'dispatched', duplicate: false, task_id: 'task-1', execution_id: 'exec-1',
      clarification: { state: 'not_needed', questions: [] }, received_at: requirement.received_at,
      source: requirement.source, message: '已派发',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('订单导出报错')).toBeTruthy())
    fireEvent.click(screen.getByText('派发执行'))
    await waitFor(() => expect(api.dispatchRequirement).toHaveBeenCalledWith('req-1', { csrfToken: 'csrf', onUnauthorized: noop }))
  })

  it('没有接收记录时显示空状态', async () => {
    api.repos.mockResolvedValue({ repos: [] })
    api.requirements.mockResolvedValue({ requirements: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有接收记录')).toBeTruthy())
  })

  it('接口失败显示真实错误', async () => {
    api.repos.mockResolvedValue({ repos: [project] })
    api.requirements.mockRejectedValue(new WorkspaceApiError(500, '服务器内部错误'))
    renderPage()
    await waitFor(() => expect(screen.getByText(/服务器内部错误/)).toBeTruthy())
  })
})
