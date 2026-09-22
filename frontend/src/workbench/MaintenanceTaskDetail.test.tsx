// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
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
    unverified: [],
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

  it('未验证项为空列表时不显示未验证警告', async () => {
    api.mockImplementation(async path => {
      if (String(path).includes('/events')) return { events: [] }
      return delivered
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByTestId('delivery-section')).toBeTruthy())
    expect(screen.queryByText('验证状态')).toBeNull()
    expect(screen.queryByText('未验证', { exact: true })).toBeNull()
  })

  it('未验证项非空时显示警告和每项具体说明', async () => {
    const unverified = ['尚未在真实达梦实例验证', '尚未进行客户环境验收']
    api.mockImplementation(async path => {
      if (String(path).includes('/events')) return { events: [] }
      return { ...delivered, delivery: { ...delivered.delivery!, unverified } }
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByTestId('delivery-section')).toBeTruthy())
    expect(screen.getByText('未验证', { exact: true })).toBeTruthy()
    for (const item of unverified) expect(screen.getByText(item)).toBeTruthy()
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

    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByTestId('clarification-panel')).toBeTruthy())
    expect(screen.getByTestId('clarification-questions').textContent).toContain('金额是按含税还是不含税口径？')

    fireEvent.change(screen.getByTestId('clarification-answer'), { target: { value: '按不含税口径' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })

    await waitFor(() => expect(screen.queryByTestId('clarification-panel')).toBeNull())
    const posted = api.mock.calls.find(c => String(c[0]).endsWith('/clarify'))
    expect(posted).toBeTruthy()
    expect(String(posted![0])).toContain('/api/v2/maintenance/tasks/')
    expect(posted![1]?.body).toEqual({ answer: '按不含税口径' })
  })

  it('没有待回答的问题时不出现这一块', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return { ...delivered, pending_questions: [] }
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(screen.queryByTestId('clarification-panel')).toBeNull()
  })
})

describe('后端给出的 actions 子集控制动作渲染', () => {
  it('给了 actions 时只按它渲染，不叠加旧的状态推断（waiting 但 actions 只放 cancel）', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return { ...waiting, actions: ['cancel'] }
    })
    renderDetail(waiting.task_id)
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    // waiting 状态原本会显示「继续执行」，但 actions 没给 resume，就不该出现。
    expect(screen.queryByText('继续执行')).toBeNull()
    expect(screen.getByText('取消任务')).toBeTruthy()
    expect(screen.queryByTestId('feedback-section')).toBeNull()
  })

  it('没有 actions 字段时保持原来按状态判断的行为（向后兼容）', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      const { actions: _drop, ...rest } = waiting as MaintenanceTaskView & { actions?: string[] }
      return rest
    })
    renderDetail(waiting.task_id)
    await waitFor(() => expect(screen.getByTestId('blocking-reason')).toBeTruthy())
    expect(screen.getByText('继续执行')).toBeTruthy()
    expect(screen.getByText('取消任务')).toBeTruthy()
  })

  it('actions 包含 feedback 时才出现后续反馈入口，提交后创建修订任务并跳转', async () => {
    const withFeedback = { ...delivered, actions: ['feedback'] }
    const revised = { ...delivered, task_id: 'mt-revision-999', actions: [] as string[] }
    api.mockImplementation(async (path, options) => {
      const url = String(path)
      if (url.endsWith('/events')) return { events: [] }
      if (options?.method === 'POST' && url.endsWith('/feedback')) return { task_id: 'mt-revision-999' }
      if (url.includes('mt-revision-999')) return revised
      return withFeedback
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByTestId('feedback-section')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText(/后续反馈/), { target: { value: '交付后还有一个问题' } })
    await act(async () => { fireEvent.click(screen.getByText('提交反馈，创建修订任务')) })

    await waitFor(() => expect(screen.getByTestId('revision-notice')).toBeTruthy())
    expect(screen.getByTestId('revision-notice').textContent).toContain('已创建修订任务，原任务保留')
    const posted = api.mock.calls.find(c => c[1]?.method === 'POST' && String(c[0]).endsWith('/feedback'))
    expect(posted).toBeTruthy()
    expect(posted![1]?.body).toEqual({ content: '交付后还有一个问题' })
  })

  it('delivered 任务默认（无 actions）不显示后续反馈入口', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return delivered
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByText('已交付')).toBeTruthy())
    expect(screen.queryByTestId('feedback-section')).toBeNull()
  })

  it('返回运维监控的链接始终存在', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return delivered
    })
    renderDetail(delivered.task_id)
    await waitFor(() => expect(screen.getByText('已交付')).toBeTruthy())
    expect(screen.getByText('← 返回运维监控').closest('a')?.getAttribute('href')).toBe('/maintenance')
  })
})
