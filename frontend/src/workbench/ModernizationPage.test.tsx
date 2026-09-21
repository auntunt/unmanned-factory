// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import ModernizationPage from './ModernizationPage'
import { request } from '../workspace/api'
import type { ModernizationSliceView } from './modernization-types'
import {
  modernizationStatusLabel,
  modernizationStatusTone,
  canCancel,
  checkHealthEvidence,
  dimensionLabel,
  pluginCreateBlockedReason,
} from './modernization-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const sampleSlice: ModernizationSliceView = {
  schema_version: 1,
  slice_id: 'mz-abc12345-6789-0000-1111-222233334444',
  revision: 1,
  status: 'running',
  project_id: 'proj-1',
  dimension: 'database',
  scope_paths: ['src/main/java/com/example/DataSourceConfig.java'],
  code_locations: [],
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  agreement: { revision: 'v1', skill_version: 'legacy-modernization@1' },
  expected_behaviour: '数据源改用达梦驱动后应用正常启动',
  delivery_goal: '补丁通过全部检查，合入主分支',
  delivery_tier: 'package',
  known_gaps: [],
  blocking_reason: null,
  steps: [{ step: 'intake', state: 'done' }, { step: 'modify', state: 'current' }],
  delivery: null,
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

const sampleProject = { id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }

function defaultRoutes(overrides: Record<string, unknown> = {}) {
  return async (path: unknown) => {
    const p = String(path)
    if (p.includes('/modernization/slices')) return overrides.slices ?? { slices: [sampleSlice] }
    if (p.includes('/modernization/availability')) return overrides.availability ?? { can_create: true, can_continue: true, state: 'enabled', name: '信创化改造' }
    if (p.includes('/modernization/targets')) return overrides.targets ?? { dimensions: [] }
    if (p === '/api/v2/projects') return overrides.projects ?? { projects: [sampleProject] }
    throw new Error(`unexpected path in test: ${p}`)
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ModernizationPage csrfToken="csrf" onUnauthorized={noop} user={{ id: '1', username: 'admin', role: 'admin' }} />
    </MemoryRouter>,
  )
}

describe('信创化改造切片列表页', () => {
  it('加载并显示切片列表', async () => {
    api.mockImplementation(defaultRoutes())
    renderPage()
    await waitFor(() => expect(screen.getByText('数据库/版本')).toBeTruthy())
    expect(screen.getAllByText('测试项目').length).toBeGreaterThan(0)
    // 改造切片按项目授权，列表请求必须带上项目
    const asked = api.mock.calls.map(c => String(c[0]))
    expect(asked.some(u => u.startsWith('/api/v2/modernization/slices?project_id=proj-1'))).toBe(true)
  })

  it('API 失败时显示错误消息而不是假成功', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/modernization/slices')) throw new Error('服务器内部错误')
      if (p.includes('/modernization/availability')) return { can_create: true, can_continue: true, state: 'enabled', name: '信创化改造' }
      if (p.includes('/modernization/targets')) return { dimensions: [] }
      return { projects: [sampleProject] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/服务器内部错误/)).toBeTruthy())
  })

  it('空列表显示空状态提示', async () => {
    api.mockImplementation(defaultRoutes({ slices: { slices: [] } }))
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有改造切片')).toBeTruthy())
  })

  it('没有可见项目时说明原因，不去请求无授权范围的切片', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p === '/api/v2/projects') return { projects: [] }
      if (p.includes('/modernization/availability')) return { can_create: true, can_continue: true, state: 'enabled', name: '信创化改造' }
      throw new Error(`unexpected path in test: ${p}`)
    })
    api.mockClear() // 只看这一次渲染发出的请求
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有可查看的项目')).toBeTruthy())
    expect(api.mock.calls.map(c => String(c[0])).some(u => u.includes('/modernization/slices'))).toBe(false)
  })

  it('插件停用时说明原因，不提供新建切片入口', async () => {
    api.mockImplementation(defaultRoutes({ availability: { can_create: false, can_continue: true, state: 'disabled', name: '信创化改造' } }))
    renderPage()
    await waitFor(() => expect(screen.getByText(/已停用，暂时不能新建任务/)).toBeTruthy())
    expect(screen.queryByText('新建切片')).toBeNull()
  })

  it('目标登记卡片和处置计划卡片都存在', async () => {
    api.mockImplementation(defaultRoutes())
    renderPage()
    await waitFor(() => expect(screen.getByTestId('dimensions-card')).toBeTruthy())
    expect(screen.getByTestId('disposal-card')).toBeTruthy()
  })
})

describe('维度标签', () => {
  it('已知维度有中文标签，未知维度原样显示', () => {
    expect(dimensionLabel('database')).toBe('数据库/版本')
    expect(dimensionLabel('never_seen')).toBe('never_seen')
  })
})

describe('改造切片状态标签', () => {
  it('每个状态都有中文标签和视觉色调', () => {
    const statuses = ['received', 'running', 'waiting', 'cancelling', 'delivered', 'failed', 'cancelled'] as const
    for (const s of statuses) {
      expect(modernizationStatusLabel(s)).not.toBe(s)
      expect(['success', 'danger', 'warning', 'active', 'neutral']).toContain(modernizationStatusTone(s))
    }
  })

  it('delivered 是 success，failed 和 cancelled 是 danger', () => {
    expect(modernizationStatusTone('delivered')).toBe('success')
    expect(modernizationStatusTone('failed')).toBe('danger')
    expect(modernizationStatusTone('cancelled')).toBe('danger')
  })
})

describe('操作权限由真实状态控制', () => {
  it('取消：received/running/waiting 可取消，其余不行', () => {
    expect(canCancel('received')).toBe(true)
    expect(canCancel('running')).toBe(true)
    expect(canCancel('waiting')).toBe(true)
    expect(canCancel('delivered')).toBe(false)
    expect(canCancel('failed')).toBe(false)
    expect(canCancel('cancelled')).toBe(false)
    expect(canCancel('cancelling')).toBe(false)
  })
})

describe('健康证据标签', () => {
  it('有 fingerprint 且未 reused → 当前', () => {
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false, identity_fingerprint: 'abc123' })).toBe('current')
  })

  it('无 fingerprint → 未检查', () => {
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false })).toBe('unchecked')
  })
})

describe('插件不可用时的新建拒绝原因', () => {
  it('禁用时说明原因', () => {
    const reason = pluginCreateBlockedReason({
      id: 'legacy-modernization', name: '信创化改造', version: 1, skill_pack_id: 'legacy-modernization',
      contract: { host: 1, min: 1, max: 1, compatible: true }, entry_points: {}, host_capabilities: [], ui_keys: [],
      executable: false, state: 'disabled', revision: 1, updated_at: null, actor: 'system',
      can_create: false, can_continue: false,
    })
    expect(reason).toMatch(/已停用/)
  })

  it('可用时返回 null', () => {
    expect(pluginCreateBlockedReason(null)).toBeNull()
  })
})
