// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import MaintenanceTaskDetail from './MaintenanceTaskDetail'
import { request } from '../workspace/api'
import type { MaintenanceTaskView } from './maintenance-types'
import { blockingTitle, isKnownBlocking } from './maintenance-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const delivered: MaintenanceTaskView = {
  schema_version: 1,
  task_id: 'mt-delivered-1234',
  revision: 2,
  status: 'delivered',
  issue: { source: 'manual', external_id: 'ext-1', version: '1', title: '修复登录异常', body: '登录时报 500' },
  issue_digest: 'digest',
  project_id: 'proj-1',
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  expected_behaviour: '登录正常工作',
  delivery_goal: '补丁合入主分支',
  delivery_tier: null,
  blocking_reason: null,
  steps: [{ step: 'intake', state: 'done' }, { step: 'modify', state: 'done' }, { step: 'verify', state: 'done' }],
  delivery: {
    commit: 'b'.repeat(40),
    checks: [
      { name: 'pytest', passed: true, exit_code: 0, reused: false, identity_fingerprint: 'fp-123' },
      { name: 'lint', passed: true, exit_code: 0, reused: true, identity_fingerprint: 'fp-456' },
      { name: 'typecheck', passed: true, exit_code: 0, reused: false },
    ],
    unverified: false,
    working_copy_base_sha: 'c'.repeat(40),
    repository: 'org/repo',
  },
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

const waiting: MaintenanceTaskView = {
  ...delivered,
  task_id: 'mt-waiting-5678',
  status: 'waiting',
  delivery: null,
  blocking_reason: { kind: 'user_input', message: '需要补充信息' },
  steps: [{ step: 'intake', state: 'done' }, { step: 'triage', state: 'blocked' }],
}

function renderDetail(taskId: string) {
  return render(
    <MemoryRouter initialEntries={[`/maintenance/${taskId}`]}>
      <Routes>
        <Route path="maintenance/:taskId" element={<MaintenanceTaskDetail csrfToken="csrf" onUnauthorized={noop} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('维护任务详情页', () => {
  it('加载并显示任务状态和基线信息', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return delivered
    })
    renderDetail('mt-delivered-1234')
    await waitFor(() => expect(screen.getAllByText('修复登录异常').length).toBeGreaterThanOrEqual(1))
    expect(screen.getByText('已交付')).toBeTruthy()
    expect(screen.getByTestId('step-list')).toBeTruthy()
  })

  it('显示交付物检查的健康证据标签', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return delivered
    })
    renderDetail('mt-delivered-1234')
    await waitFor(() => expect(screen.getByTestId('checks-table')).toBeTruthy())
    // pytest: fingerprint + not reused → 当前
    expect(screen.getByTestId('evidence-current')).toBeTruthy()
    // lint: fingerprint + reused → 过期
    expect(screen.getByTestId('evidence-stale')).toBeTruthy()
    // typecheck: no fingerprint → 未检查
    expect(screen.getByTestId('evidence-unchecked')).toBeTruthy()
  })

  it('waiting 状态显示阻塞原因和继续/取消按钮', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return waiting
    })
    renderDetail('mt-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    expect(screen.getByText(/需要补充信息/)).toBeTruthy()
    expect(screen.getByText('继续执行')).toBeTruthy()
    expect(screen.getByText('取消任务')).toBeTruthy()
  })

  it('等待但没有可继续的现场时，不给一个点了必然被拒的按钮', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return { ...waiting, resumable: false }
    })
    renderDetail('mt-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    expect(screen.queryByText('继续执行')).toBeNull()
    expect(screen.getByText('取消任务')).toBeTruthy()
  })

  it('已知阻塞原因显示中文标题，原始代码仍然留在详情里', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return {
        ...waiting,
        blocking_reason: { kind: 'approval.required', message: '计划已就绪，等待人工批准后才会开始改动' },
      }
    })
    renderDetail('mt-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    const block = screen.getByTestId('blocking-reason').textContent ?? ''
    expect(block).toContain('等待批准执行计划')
    // 中文标题是加上去的，不是把原始代码换掉：它仍然可被引用。
    expect(screen.getByTestId('blocking-kind').textContent).toContain('approval.required')
    expect(block).not.toMatch(/阻塞原因：approval\.required/)
  })

  it('不认识的阻塞种类原样显示，不编一个中文说法', () => {
    expect(blockingTitle('run.failed')).toBe('执行失败，停下等人处理')
    expect(blockingTitle('never.seen.this')).toBe('never.seen.this')
    expect(isKnownBlocking('never.seen.this')).toBe(false)
  })

  it('执行阶段显示 SOP 步骤名和中文状态，不回显服务端的键', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return waiting
    })
    renderDetail('mt-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('step-list')).toBeTruthy())
    const steps = screen.getByTestId('step-list').textContent ?? ''
    expect(steps).toContain('接收登记')
    expect(steps).toContain('定位分诊')
    expect(steps).toContain('等待人工')
    expect(steps).not.toContain('intake')
    expect(steps).not.toContain('blocked')
  })

  it('delivered 状态不显示取消或继续按钮', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return delivered
    })
    renderDetail('mt-delivered-1234')
    await waitFor(() => expect(screen.getByText('已交付')).toBeTruthy())
    expect(screen.queryByText('取消任务')).toBeNull()
    expect(screen.queryByText('继续执行')).toBeNull()
  })

  it('API 错误显示真实错误而不是假数据', async () => {
    api.mockImplementation(async () => { throw new Error('任务不存在') })
    renderDetail('mt-nonexistent')
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/任务不存在/)).toBeTruthy()
  })

  it('补充表单存在且可提交', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      if (typeof path === 'string' && path.includes('/follow-up')) return { recorded: true, applied: false }
      return waiting
    })
    renderDetail('mt-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('followup-form')).toBeTruthy())
    const textarea = screen.getByPlaceholderText(/补充说明/)
    fireEvent.change(textarea, { target: { value: '补充信息内容' } })
    const submitButton = screen.getByText('提交补充')
    fireEvent.click(submitButton)
    await waitFor(() => expect(screen.getByTestId('followup-receipt')).toBeTruthy())
    expect(screen.getByText(/已记录/)).toBeTruthy()
  })

  it('不显示进度百分比或进度条（F3：无可靠分母时只显示阶段）', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return delivered
    })
    renderDetail('mt-delivered-1234')
    await waitFor(() => expect(screen.getByTestId('progress-section')).toBeTruthy())
    const section = screen.getByTestId('progress-section')
    expect(section.textContent).not.toMatch(/%/)
    expect(section.querySelector('progress')).toBeNull()
  })
})
