// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import PluginsSettings from './PluginsSettings'
import MaintenanceTasksPage from './MaintenanceTasksPage'
import { request } from '../workspace/api'
import type { PluginAvailabilityView, PluginState } from './maintenance-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(() => { cleanup(); api.mockReset() })

function availability(state: PluginState, over: Partial<PluginAvailabilityView> = {}): PluginAvailabilityView {
  return {
    id: 'issue-maintenance', name: '自动化运维', version: 1,
    skill_pack_id: 'issue-maintenance',
    contract: { host: 1, min: 1, max: 1, compatible: true },
    entry_points: { http: '/api/v2/maintenance', cli: 'maintenance-cli', ui: '/maintenance' },
    host_capabilities: ['project.authorize', 'run.dispatch'],
    ui_keys: ['maintenance.tasks'],
    executable: true,
    state, revision: 3, updated_at: '2026-09-21T10:00:00Z', actor: 'owner',
    can_create: state === 'enabled',
    can_continue: state !== 'disabled',
    ...over,
  }
}

const admin = { id: '1', username: 'admin', role: 'admin' as const }

function renderSettings(user = admin) {
  return render(
    <MemoryRouter>
      <PluginsSettings csrfToken="csrf" onUnauthorized={noop} user={user} />
    </MemoryRouter>,
  )
}

function renderMaintenance(state: PluginState) {
  api.mockImplementation(async (path) => {
    const url = String(path)
    if (url.includes('/maintenance/availability')) return availability(state)
    if (url.includes('/maintenance/tasks')) return { tasks: [], availability: availability(state) }
    if (url.includes('/maintenance/agreement')) return { revision: 'v1', skill_version: 'issue-maintenance@1' }
    return { projects: [{ id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }] }
  })
  return render(
    <MemoryRouter>
      <MaintenanceTasksPage csrfToken="csrf" onUnauthorized={noop} user={admin} />
    </MemoryRouter>,
  )
}

describe('业务插件设置页', () => {
  it('列出插件及其状态，未接入处理器的不给启用按钮', async () => {
    api.mockResolvedValue({ plugins: [
      availability('enabled'),
      availability('disabled', { id: 'api-adaptation', name: '自动化三方接口适配',
        skill_pack_id: 'api-adaptation', executable: false, entry_points: {},
        host_capabilities: [], can_create: false, can_continue: false }),
    ] })
    renderSettings()
    await waitFor(() => expect(screen.getByTestId('plugin-issue-maintenance')).toBeTruthy())
    expect(screen.getByText('已启用')).toBeTruthy()
    const unwired = screen.getByTestId('plugin-api-adaptation')
    expect(unwired.textContent).toContain('尚未接入可执行处理器')
    const enableButton = Array.from(unwired.querySelectorAll('button'))
      .find(b => b.textContent?.includes('重新启用'))
    expect(enableButton?.disabled).toBe(true)
  })

  it('停用要先排空：已启用时只提供「开始排空」', async () => {
    api.mockResolvedValue({ plugins: [availability('enabled')] })
    renderSettings()
    await waitFor(() => expect(screen.getByTestId('plugin-issue-maintenance')).toBeTruthy())
    const labels = Array.from(
      screen.getByTestId('plugin-issue-maintenance').querySelectorAll('button'),
    ).map(b => b.textContent)
    expect(labels.some(l => l?.includes('开始排空'))).toBe(true)
    expect(labels.some(l => l?.includes('停用'))).toBe(false)
  })

  it('改状态时带上读到的 revision，两个管理员不会互相无声覆盖', async () => {
    api.mockImplementation(async (path, options) => {
      if (String(path) === '/api/v2/plugins') return { plugins: [availability('enabled')] }
      expect(options?.body).toEqual({ state: 'draining', expected_revision: 3 })
      return availability('draining', { revision: 4 })
    })
    renderSettings()
    await waitFor(() => expect(screen.getByTestId('plugin-issue-maintenance')).toBeTruthy())
    const drain = Array.from(
      screen.getByTestId('plugin-issue-maintenance').querySelectorAll('button'),
    ).find(b => b.textContent?.includes('开始排空'))!
    await act(async () => { fireEvent.click(drain) })
    await waitFor(() => expect(screen.getByText('排空中')).toBeTruthy())
  })

  it('服务端拒绝时原样显示，不当作已生效', async () => {
    const { WorkspaceApiError } = await vi.importActual<typeof import('../workspace/api')>('../workspace/api')
    api.mockImplementation(async (path) => {
      if (String(path) === '/api/v2/plugins') return { plugins: [availability('draining')] }
      throw new WorkspaceApiError(409, '自动化运维还有 2 个执行没有结束，清空后才能停用；本操作不会自动取消客户任务')
    })
    renderSettings()
    await waitFor(() => expect(screen.getByTestId('plugin-issue-maintenance')).toBeTruthy())
    const stop = Array.from(
      screen.getByTestId('plugin-issue-maintenance').querySelectorAll('button'),
    ).find(b => b.textContent?.includes('停用'))!
    await act(async () => { fireEvent.click(stop) })
    await waitFor(() => expect(screen.getByText(/还有 2 个执行没有结束/)).toBeTruthy())
    // 状态没有被前端乐观地改掉
    expect(screen.getByText('排空中')).toBeTruthy()
  })
})

describe('维护任务页跟随插件可用性', () => {
  it('启用时提供新建入口', async () => {
    renderMaintenance('enabled')
    await waitFor(() => expect(screen.getByRole('button', { name: /新建任务/ })).toBeTruthy())
    expect(screen.queryByText(/正在排空/)).toBeNull()
  })

  it('排空中不摆一个必然失败的新建按钮，并说明原因', async () => {
    renderMaintenance('draining')
    await waitFor(() => expect(screen.getByText(/正在排空/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /新建任务/ })).toBeNull()
    expect(screen.getByText(/已有任务仍可查询、导出与取消/)).toBeTruthy()
  })

  it('已停用时说明历史仍可查看', async () => {
    renderMaintenance('disabled')
    await waitFor(() => expect(screen.getByText(/已停用/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /新建任务/ })).toBeNull()
    expect(screen.getByText(/历史任务与交付物仍然可以查看和下载/)).toBeTruthy()
  })
})
