// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import ModernizationSliceDetail from './ModernizationSliceDetail'
import { request } from '../workspace/api'
import type { ModernizationSliceView } from './modernization-types'
import { blockingTitle, isKnownBlocking, nextAction } from './modernization-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const delivered: ModernizationSliceView = {
  schema_version: 1,
  slice_id: 'mz-delivered-1234',
  revision: 2,
  status: 'delivered',
  project_id: 'proj-1',
  dimension: 'database',
  scope_paths: ['src/main/java/com/example/DataSourceConfig.java'],
  code_locations: [{ path: 'src/main/java/com/example/DataSourceConfig.java', line: 42, source: 'codegraph' }],
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  agreement: { revision: 'v1', skill_version: 'legacy-modernization@1' },
  expected_behaviour: '数据源改用达梦驱动后应用正常启动',
  delivery_goal: '补丁通过全部检查，合入主分支',
  delivery_tier: 'package',
  known_gaps: ['无达梦实例，数据库验证无法在本轮完成'],
  blocking_reason: null,
  steps: [{ step: 'intake', state: 'done' }, { step: 'modify', state: 'done' }, { step: 'verify', state: 'done' }],
  delivery: {
    commit: 'b'.repeat(40),
    checks: [
      { name: 'pytest', passed: true, exit_code: 0, reused: false, identity_fingerprint: 'fp-123' },
      { name: 'lint', passed: true, exit_code: 0, reused: true, identity_fingerprint: 'fp-456' },
      { name: 'typecheck', passed: true, exit_code: 0, reused: false },
    ],
    unverified: ['数据库/版本未在真实目标环境验证：本轮检查未覆盖，不能标记为验证通过'],
    working_copy_base_sha: 'c'.repeat(40),
    repository: 'org/repo',
  },
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

const waiting: ModernizationSliceView = {
  ...delivered,
  slice_id: 'mz-waiting-5678',
  status: 'waiting',
  delivery: null,
  blocking_reason: { kind: 'clarification.requested', message: '需要补充信息' },
  steps: [{ step: 'intake', state: 'done' }, { step: 'locate', state: 'blocked' }],
}

const awaitingApproval: ModernizationSliceView = {
  ...waiting,
  slice_id: 'mz-approval-9999',
  blocking_reason: { kind: 'approval.requested', message: '计划已就绪，等待人工批准后才会开始改动' },
}

function availabilityResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return { can_create: true, can_continue: true, state: 'enabled', name: '信创化改造', ...overrides }
}

function renderDetail(sliceId: string) {
  return render(
    <MemoryRouter initialEntries={[`/modernization/${sliceId}`]}>
      <Routes>
        <Route path="modernization/:sliceId" element={<ModernizationSliceDetail csrfToken="csrf" onUnauthorized={noop} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('信创化改造切片详情页', () => {
  it('加载并显示切片状态和基线信息', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return delivered
    })
    renderDetail('mz-delivered-1234')
    await waitFor(() => expect(screen.getByText('已交付')).toBeTruthy())
    expect(screen.getByTestId('step-list')).toBeTruthy()
    expect(screen.getAllByText('org/repo').length).toBeGreaterThan(0)
  })

  it('显示交付物检查的健康证据标签和未验证条件列表', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return delivered
    })
    renderDetail('mz-delivered-1234')
    await waitFor(() => expect(screen.getByTestId('checks-table')).toBeTruthy())
    expect(screen.getByTestId('evidence-current')).toBeTruthy()
    expect(screen.getByTestId('evidence-stale')).toBeTruthy()
    expect(screen.getByTestId('evidence-unchecked')).toBeTruthy()
    expect(screen.getByTestId('unverified-list').textContent).toContain('数据库/版本未在真实目标环境验证')
  })

  it('waiting + clarification.requested 给的是回答入口，不是「继续执行」', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return waiting
    })
    renderDetail('mz-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    expect(screen.getByTestId('blocking-message').textContent).toContain('需要补充信息')
    // 停在提问上时「继续执行」只会把运行推回同一个闸门，摆出来就是误导：
    // 要的是回答，不是 resume。
    expect(screen.queryByText('继续执行')).toBeNull()
    expect(screen.getByText('取消切片')).toBeTruthy()
    expect(screen.queryByText('批准计划')).toBeNull()
  })

  it('waiting + approval.requested 显示批准计划按钮而不是继续执行', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return awaitingApproval
    })
    renderDetail('mz-approval-9999')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    expect(screen.getByText('批准计划')).toBeTruthy()
    expect(screen.queryByText('继续执行')).toBeNull()
  })

  it('nextAction 只依据服务端给的 blocking_reason.kind 判断', () => {
    expect(nextAction('waiting', { kind: 'approval.requested', message: '' })).toBe('approve')
    expect(nextAction('waiting', { kind: 'clarification.requested', message: '' })).toBe('clarify')
    expect(nextAction('waiting', null)).toBe('resume')
    expect(nextAction('running', null)).toBeNull()
  })

  it('已知阻塞原因显示中文标题，原始代码仍然留在详情里', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return awaitingApproval
    })
    renderDetail('mz-approval-9999')
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    const block = screen.getByTestId('blocking-reason').textContent ?? ''
    expect(block).toContain('计划已就绪')
    expect(screen.getByTestId('blocking-kind').textContent).toContain('approval.requested')
  })

  it('不认识的阻塞种类原样显示，不编一个中文说法', () => {
    expect(blockingTitle('run.failed')).toBe('执行失败，停下等人处理')
    expect(blockingTitle('never.seen.this')).toBe('never.seen.this')
    expect(isKnownBlocking('never.seen.this')).toBe(false)
  })

  it('delivered 状态不显示取消或继续/批准按钮', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return delivered
    })
    renderDetail('mz-delivered-1234')
    await waitFor(() => expect(screen.getByText('已交付')).toBeTruthy())
    expect(screen.queryByText('取消切片')).toBeNull()
    expect(screen.queryByText('继续执行')).toBeNull()
    expect(screen.queryByText('批准计划')).toBeNull()
  })

  it('API 错误显示真实错误而不是假数据', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/availability')) return availabilityResponse()
      throw new Error('切片不存在')
    })
    renderDetail('mz-nonexistent')
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/切片不存在/)).toBeTruthy()
  })

  it('继续反馈：提交修订表单存在，提交后跳到新修订', async () => {
    const revised: ModernizationSliceView = { ...waiting, slice_id: 'mz-revised-0001', predecessor_id: 'mz-waiting-5678' }
    api.mockImplementation(async (path, options) => {
      const p = String(path)
      if (p.includes('/revise')) return revised
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      void options
      return waiting
    })
    renderDetail('mz-waiting-5678')
    await waitFor(() => expect(screen.getByText('提交修订')).toBeTruthy())
    fireEvent.click(screen.getByText('提交修订'))
    await waitFor(() => expect(screen.getByTestId('revise-form')).toBeTruthy())
    fireEvent.click(screen.getByText('提交新修订'))
    await waitFor(() => expect(api.mock.calls.some(c => String(c[0]).includes('/revise'))).toBe(true))
  })

  it('插件停用（create 类）时不提供提交修订入口', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse({ can_create: false, state: 'disabled' })
      return waiting
    })
    renderDetail('mz-waiting-5678')
    await waitFor(() => expect(screen.getByTestId('revise-section')).toBeTruthy())
    expect(screen.queryByText('提交修订')).toBeNull()
    expect(screen.getByText(/已停用，暂时不能新建任务/)).toBeTruthy()
  })

  it('代码定位带来源，显示在范围卡片里', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return delivered
    })
    renderDetail('mz-delivered-1234')
    await waitFor(() => expect(screen.getByTestId('code-locations')).toBeTruthy())
    expect(screen.getByTestId('code-locations').textContent).toContain('codegraph')
  })

  it('不显示进度百分比或进度条', async () => {
    api.mockImplementation(async (path) => {
      const p = String(path)
      if (p.includes('/events')) return { events: [] }
      if (p.includes('/availability')) return availabilityResponse()
      return delivered
    })
    renderDetail('mz-delivered-1234')
    await waitFor(() => expect(screen.getByTestId('progress-section')).toBeTruthy())
    const section = screen.getByTestId('progress-section')
    expect(section.textContent).not.toMatch(/%/)
    expect(section.querySelector('progress')).toBeNull()
  })
})


describe('模型提问时的回答入口', () => {
  it('展示服务端给的问题，回答后用读回的真实状态刷新，刷新后未回答的问题仍在', async () => {
    const waiting = {
      ...delivered, status: 'waiting', pending_questions: ['金额是按含税还是不含税口径？'],
      blocking_reason: { kind: 'clarification.requested', message: '模型提出了需要确认的问题', event: null },
    }
    const answered = { ...waiting, status: 'running', pending_questions: [], blocking_reason: null }
    let answeredOnce = false
    api.mockImplementation(async (path, options) => {
      const url = String(path)
      if (url.endsWith('/events')) return { events: [] }
      if (options?.method === 'POST' && url.endsWith('/clarify')) {
        answeredOnce = true
        return answered
      }
      return answeredOnce ? answered : waiting
    })

    renderDetail(delivered.slice_id)
    await waitFor(() => expect(screen.getByTestId('clarification-panel')).toBeTruthy())
    expect(screen.getByTestId('clarification-questions').textContent).toContain('金额是按含税还是不含税口径？')

    fireEvent.change(screen.getByTestId('clarification-answer'), { target: { value: '按不含税口径' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })

    await waitFor(() => expect(screen.queryByTestId('clarification-panel')).toBeNull())
    const posted = api.mock.calls.find(c => String(c[0]).endsWith('/clarify'))
    expect(posted).toBeTruthy()
    expect(String(posted![0])).toContain('/api/v2/modernization/slices/')
    expect(posted![1]?.body).toEqual({ answer: '按不含税口径' })
  })

  it('没有待回答的问题时不出现这一块', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return { ...delivered, pending_questions: [] }
    })
    renderDetail(delivered.slice_id)
    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(screen.queryByTestId('clarification-panel')).toBeNull()
  })
})
