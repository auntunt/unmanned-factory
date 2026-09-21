// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import AdaptationPage from './AdaptationPage'
import { request } from '../workspace/api'
import type { AdaptationTaskView, PluginAvailabilityView } from './adaptation-types'
import {
  adaptationStatusLabel,
  adaptationStatusTone,
  canCancelAdaptation,
  pluginCreateBlockedReason,
} from './adaptation-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const sampleTask: AdaptationTaskView = {
  schema_version: 1,
  task_id: 'ad-abc12345-6789-0000-1111-222233334444',
  revision: 1,
  predecessor_id: null,
  successor_id: null,
  status: 'running',
  project_id: 'proj-1',
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  contract: {
    source_api: { name: '报关接口', version: '1.0', base_url: 'https://mock.local/customs', auth: 'apikey', environment: 'mock' },
    target_adapter: { name: 'customs-adapter', entry: 'adapters/customs.py:handle' },
    endpoints: [],
    fields: {},
  },
  contract_digest: 'digest-1',
  contract_diff: null,
  affected_mappings: [],
  mapping_matrix: [
    { source_field: 'f1', mapped: true, target_field: 't1', type: '', unit: '', enum: [], null_semantics: '', precision: '', encoding: '', timezone: '', error_code: '', transform: '', impact: '', notes: '' },
  ],
  unmapped_fields: [],
  counts: { http_operations: 0, business_messages: 1, fields: 1 },
  message: { name: '报关请求', method: 'POST', path: '/customs', idempotent: true, retry_policy: '', sample_payload: {} },
  agreement: { revision: '1', skill_version: '1' },
  expected_behaviour: '报关正常提交',
  delivery_goal: '补丁合入',
  delivery_tier: 'package',
  synthetic: true,
  execution_id: 'exec-1',
  cost_usd: 0.1,
  delivery: null,
  business_call: null,
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

const project = { id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }

const availableView: PluginAvailabilityView = {
  id: 'api-adaptation', name: '自动化三方接口适配', version: 1, skill_pack_id: 'api-adaptation',
  contract: { host: 1, min: 1, max: 1, compatible: true }, entry_points: {}, host_capabilities: [], ui_keys: [],
  executable: true, state: 'enabled', revision: 1, updated_at: null, actor: 'system',
  can_create: true, can_continue: true,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AdaptationPage csrfToken="csrf" onUnauthorized={noop} user={{ id: '1', username: 'admin', role: 'admin' }} />
    </MemoryRouter>,
  )
}

describe('接口适配任务列表页', () => {
  it('加载并显示任务列表', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [sampleTask] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/报关接口/)).toBeTruthy())
    expect(screen.getAllByText('测试项目').length).toBeGreaterThan(0)
    const asked = api.mock.calls.map(c => String(c[0]))
    expect(asked.some(u => u.startsWith('/api/v2/adaptation/tasks?project_id=proj-1'))).toBe(true)
  })

  it('API 失败时显示错误消息而不是假成功', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) throw new Error('服务器内部错误')
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/服务器内部错误/)).toBeTruthy()
  })

  it('空列表显示空状态提示', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有适配任务')).toBeTruthy())
  })

  it('插件停用时不提供新建入口，但仍能看任务', async () => {
    const disabled: PluginAvailabilityView = { ...availableView, state: 'disabled', executable: false, can_create: false }
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [sampleTask] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return disabled
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/报关接口/)).toBeTruthy())
    await waitFor(() => expect(screen.queryByText('新建任务')).toBeNull())
    expect(screen.getByText(/已停用/)).toBeTruthy()
  })

  it('点击新建任务展开新建表单，可以取消关闭', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('新建任务')).toBeTruthy())
    fireEvent.click(screen.getByText('新建任务'))
    await waitFor(() => expect(screen.getByText('新建接口适配任务')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('关闭'))
    await waitFor(() => expect(screen.queryByText('新建接口适配任务')).toBeNull())
  })

  it('没有后继修订的任务提供"导入新版本"入口', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [sampleTask] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('导入新版本')).toBeTruthy())
  })

  it('已有后继修订的任务不提供"导入新版本"入口', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/adaptation/tasks')) return { tasks: [{ ...sampleTask, successor_id: 'ad-next' }] }
      if (typeof path === 'string' && path.includes('/adaptation/availability')) return availableView
      return { projects: [project] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/报关接口/)).toBeTruthy())
    expect(screen.queryByText('导入新版本')).toBeNull()
  })

  it('没有可见项目时说明原因，不去请求无授权范围的任务', async () => {
    api.mockImplementation(async () => ({ projects: [] }) as never)
    api.mockClear()
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有可查看的项目')).toBeTruthy())
    expect(api.mock.calls.map(c => String(c[0])).some(u => u.includes('/adaptation/tasks'))).toBe(false)
  })
})

describe('接口适配状态标签', () => {
  it('每个状态都有中文标签和视觉色调', () => {
    const statuses = ['received', 'running', 'waiting', 'cancelling', 'delivered', 'failed', 'cancelled'] as const
    for (const s of statuses) {
      expect(adaptationStatusLabel(s)).not.toBe(s)
      expect(['success', 'danger', 'warning', 'active', 'neutral']).toContain(adaptationStatusTone(s))
    }
  })
})

describe('取消操作权限由真实状态控制', () => {
  it('received/running/waiting 可取消，其余不行', () => {
    expect(canCancelAdaptation('received')).toBe(true)
    expect(canCancelAdaptation('running')).toBe(true)
    expect(canCancelAdaptation('waiting')).toBe(true)
    expect(canCancelAdaptation('delivered')).toBe(false)
    expect(canCancelAdaptation('failed')).toBe(false)
    expect(canCancelAdaptation('cancelled')).toBe(false)
    expect(canCancelAdaptation('cancelling')).toBe(false)
  })
})

describe('新建/修订入口的可用性判断', () => {
  it('可用时不阻塞', () => {
    expect(pluginCreateBlockedReason(availableView)).toBeNull()
  })
  it('排空中给出可操作的说明', () => {
    const draining = { ...availableView, state: 'draining' as const, can_create: false }
    expect(pluginCreateBlockedReason(draining)).toContain('排空')
  })
  it('停用给出可操作的说明', () => {
    const disabled = { ...availableView, state: 'disabled' as const, can_create: false }
    expect(pluginCreateBlockedReason(disabled)).toContain('已停用')
  })
})
