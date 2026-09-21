// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MaintenanceTasksPage from './MaintenanceTasksPage'
import { request } from '../workspace/api'
import type { MaintenanceTaskView } from './maintenance-types'
import {
  maintenanceStatusLabel,
  maintenanceStatusTone,
  canCancel,
  canResume,
  checkHealthEvidence,
  supplementStatus,
  supplementStatusLabel,
} from './maintenance-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const sampleTask: MaintenanceTaskView = {
  schema_version: 1,
  task_id: 'mt-abc12345-6789-0000-1111-222233334444',
  revision: 1,
  status: 'running',
  issue: { source: 'manual', external_id: 'ext-1', version: '1', title: '修复登录异常', body: '详细描述' },
  issue_digest: 'digest123',
  project_id: 'proj-1',
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  expected_behaviour: '登录正常',
  delivery_goal: '补丁合入',
  delivery_tier: null,
  blocking_reason: null,
  steps: [{ step: 'intake', state: 'done' }, { step: 'modify', state: 'current' }],
  delivery: null,
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <MaintenanceTasksPage csrfToken="csrf" onUnauthorized={noop} user={{ id: '1', username: 'admin', role: 'admin' }} />
    </MemoryRouter>,
  )
}

describe('维护任务列表页', () => {
  it('加载并显示任务列表', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/maintenance')) return { tasks: [sampleTask] }
      return { projects: [{ id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('修复登录异常')).toBeTruthy())
    // 项目名出现在选择器和表格里，所以这里不能用 getByText
    expect(screen.getAllByText('测试项目').length).toBeGreaterThan(0)
    // 维护任务按项目授权，列表必须带上项目；不带会被后端拒成 422
    const asked = api.mock.calls.map(c => String(c[0]))
    expect(asked.some(u => u.startsWith('/api/v2/maintenance/tasks?project_id=proj-1'))).toBe(true)
  })

  it('API 失败时显示错误消息而不是假成功', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/maintenance')) throw new Error('服务器内部错误')
      return { projects: [{ id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/服务器内部错误/)).toBeTruthy()
  })

  it('空列表显示空状态提示', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/maintenance')) return { tasks: [] }
      return { projects: [{ id: 'proj-1', name: '测试项目', repository: 'org/repo', workspace: 'w', base_branch: 'main', checks: {}, auto_issues: false, auto_publish: false }] }
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有维护任务')).toBeTruthy())
  })

  it('没有可见项目时说明原因，不去请求无授权范围的任务', async () => {
    api.mockImplementation(async () => ({ projects: [] }) as never)
    api.mockClear() // 只看这一次渲染发出的请求
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有可查看的项目')).toBeTruthy())
    expect(api.mock.calls.map(c => String(c[0]))
      .some(u => u.includes('/maintenance/tasks'))).toBe(false)
  })
})

describe('维护任务状态标签', () => {
  it('每个状态都有中文标签和视觉色调', () => {
    const statuses = ['received', 'running', 'waiting', 'cancelling', 'delivered', 'failed', 'cancelled'] as const
    for (const s of statuses) {
      const label = maintenanceStatusLabel(s)
      const tone = maintenanceStatusTone(s)
      expect(label).not.toBe(s) // 不是原文回显
      expect(['success', 'danger', 'warning', 'active', 'neutral']).toContain(tone)
    }
  })

  it('running 和 waiting 有不同的色调', () => {
    expect(maintenanceStatusTone('running')).not.toBe(maintenanceStatusTone('waiting'))
  })

  it('delivered 是 success，failed 和 cancelled 是 danger', () => {
    expect(maintenanceStatusTone('delivered')).toBe('success')
    expect(maintenanceStatusTone('failed')).toBe('danger')
    expect(maintenanceStatusTone('cancelled')).toBe('danger')
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

  it('继续：只有 waiting 可继续', () => {
    expect(canResume('waiting')).toBe(true)
    expect(canResume('received')).toBe(false)
    expect(canResume('running')).toBe(false)
    expect(canResume('delivered')).toBe(false)
  })
})

describe('健康证据标签 (F3)', () => {
  it('有 fingerprint 且未 reused → 当前', () => {
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false, identity_fingerprint: 'abc123' })).toBe('current')
  })

  it('有 fingerprint 且 reused → 过期', () => {
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: true, identity_fingerprint: 'abc123' })).toBe('stale')
  })

  it('无 fingerprint → 未检查（不是"相同"）', () => {
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false })).toBe('unchecked')
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false, identity_fingerprint: null })).toBe('unchecked')
    expect(checkHealthEvidence({ name: 'test', passed: true, exit_code: 0, reused: false, identity_fingerprint: '' })).toBe('unchecked')
  })
})

describe('补充状态标签 (F3)', () => {
  it('applied 是已应用', () => {
    expect(supplementStatus({ recorded: true, applied: true })).toBe('applied')
    expect(supplementStatusLabel('applied')).toBe('已应用')
  })

  it('recorded 但未 applied 是已记录', () => {
    expect(supplementStatus({ recorded: true })).toBe('recorded')
    expect(supplementStatusLabel('recorded')).toBe('已记录')
  })

  it('未 recorded 是待应用', () => {
    expect(supplementStatus({ recorded: false })).toBe('pending')
    expect(supplementStatusLabel('pending')).toBe('待应用')
  })
})
