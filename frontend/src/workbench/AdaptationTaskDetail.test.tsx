// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import AdaptationTaskDetail from './AdaptationTaskDetail'
import { request } from '../workspace/api'
import type { AdaptationTaskView } from './adaptation-types'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const baseTask: AdaptationTaskView = {
  schema_version: 1,
  task_id: 'ad-task-0001',
  revision: 1,
  predecessor_id: null,
  successor_id: null,
  status: 'delivered',
  project_id: 'proj-1',
  baseline: { repository: 'org/repo', base_sha: 'a'.repeat(40), base_branch_label: 'main' },
  contract: {
    source_api: { name: '发票开具接口', version: '2.0', base_url: 'https://mock.local/invoice', auth: 'bearer', environment: 'mock' },
    target_adapter: { name: 'invoice-adapter', entry: 'adapters/invoice.py:handle' },
    endpoints: [{ path: '/invoice', method: 'POST' }],
    fields: { order_id: { type: 'string' }, legacy_flag: { type: 'boolean' } },
  },
  contract_digest: 'digest-abc',
  contract_diff: null,
  affected_mappings: [],
  mapping_matrix: [
    { source_field: 'order_id', mapped: true, target_field: 'orderId', type: 'string', unit: '', enum: [], null_semantics: '', precision: '', encoding: '', timezone: '', error_code: '', transform: '', impact: '', notes: '' },
    { source_field: 'legacy_flag', mapped: false, target_field: '', type: '', unit: '', enum: [], null_semantics: '', precision: '', encoding: '', timezone: '', error_code: '', transform: '', impact: '目标系统无对应字段，将按默认关闭处理，可能影响历史订单展示', notes: '' },
  ],
  unmapped_fields: [
    { source_field: 'legacy_flag', mapped: false, target_field: '', type: '', unit: '', enum: [], null_semantics: '', precision: '', encoding: '', timezone: '', error_code: '', transform: '', impact: '目标系统无对应字段，将按默认关闭处理，可能影响历史订单展示', notes: '' },
  ],
  counts: { http_operations: 1, business_messages: 1, fields: 2 },
  message: { name: '开票请求', method: 'POST', path: '/invoice', idempotent: false, retry_policy: '按幂等键去重，超时不盲重试', sample_payload: { order_id: 'o-1' } },
  agreement: { revision: '1', skill_version: '1' },
  expected_behaviour: '正确开具发票并返回业务受理结果',
  delivery_goal: '补丁通过检查，真实调用一次开票请求',
  delivery_tier: 'package',
  synthetic: true,
  execution_id: 'exec-1',
  cost_usd: 0.5,
  delivery: {
    commit: 'b'.repeat(40),
    checks: [{ name: 'pytest', passed: true, exit_code: 0, reused: false, identity_fingerprint: 'fp-1' }],
    unverified: [],
    working_copy_base_sha: 'a'.repeat(40),
    repository: 'org/repo',
    synthetic: true,
    business_call: {
      correlation_id: 'corr-123',
      technical_success: true,
      business_accepted: true,
      business_completed: false,
      mock: true,
      endpoint: 'https://mock.local/invoice',
      sanitized_response: { code: '0000' },
    },
  },
  business_call: {
    correlation_id: 'corr-123',
    technical_success: true,
    business_accepted: true,
    business_completed: false,
    mock: true,
    endpoint: 'https://mock.local/invoice',
    sanitized_response: { code: '0000' },
  },
  receipts: [],
  created_at: '2026-09-21T10:00:00Z',
}

function renderDetail(taskId: string) {
  return render(
    <MemoryRouter initialEntries={[`/adaptation/${taskId}`]}>
      <Routes>
        <Route path="adaptation/:taskId" element={<AdaptationTaskDetail csrfToken="csrf" onUnauthorized={noop} />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('接口适配任务详情页', () => {
  it('加载并显示契约、状态与基线信息', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return baseTask
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getAllByText(/发票开具接口/).length).toBeGreaterThanOrEqual(1))
    expect(screen.getByText('已交付')).toBeTruthy()
    expect(screen.getByTestId('contract-section')).toBeTruthy()
  })

  it('字段映射矩阵把待确认字段单独展示，并写明影响', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return baseTask
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('mapping-section')).toBeTruthy())
    // 已映射字段出现在已映射表格里
    expect(screen.getByTestId('mapped-table').textContent).toContain('order_id')
    expect(screen.getByTestId('mapped-table').textContent).not.toContain('legacy_flag')
    // 待确认字段单独出现在自己的区块，附带影响说明
    const unmapped = screen.getByTestId('unmapped-list')
    expect(unmapped.textContent).toContain('legacy_flag')
    expect(unmapped.textContent).toContain('目标系统无对应字段')
    expect(screen.getByTestId('unmapped-section').textContent).toContain('待确认')
  })

  it('business_completed=false 时不显示为已完成，且 mock 标记可见', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return baseTask
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('business-call-section')).toBeTruthy())
    const section = screen.getByTestId('business-call-section')
    // 三项分开展示：技术成功、业务受理都为 true，但业务完成是 false
    expect(section.textContent).toContain('技术是否成功')
    expect(section.textContent).toContain('业务是否受理')
    expect(section.textContent).toContain('业务是否最终完成')
    expect(section.textContent).toContain('业务未完成')
    // 绝不能把这条 200/mock 成功的调用呈现成"业务已完成"
    expect(section.textContent).not.toContain('业务已完成')
    // mock 标记必须可见，且带有说明它不是真实第三方
    const marker = screen.getByTestId('mock-marker')
    expect(marker.textContent).toContain('本地模拟')
    // 有一条解释：完成状态为 false 不等于业务受理或技术成功
    expect(screen.getByTestId('business-completed-caveat').textContent).toContain('不等同于业务受理或技术成功')
  })

  it('未联调时明确显示"未联调"，不编造业务结果', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return { ...baseTask, business_call: null, delivery: { ...baseTask.delivery!, business_call: null } }
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('business-call-section')).toBeTruthy())
    expect(screen.getByTestId('no-business-call').textContent).toContain('未联调')
    expect(screen.queryByTestId('mock-marker')).toBeNull()
  })

  it('真实环境联调时 mock 标记显示为真实第三方', async () => {
    const realCall = { ...baseTask.business_call!, mock: false, business_completed: true }
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return { ...baseTask, business_call: realCall, delivery: { ...baseTask.delivery!, business_call: realCall } }
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('mock-marker')).toBeTruthy())
    expect(screen.getByTestId('mock-marker').textContent).toContain('真实第三方')
    expect(screen.getByTestId('business-call-section').textContent).toContain('业务已完成')
  })

  it('契约差异存在时显示新增/移除/变化字段与受影响映射', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return {
        ...baseTask,
        contract_diff: { source_api_version: { old: '1.0', new: '2.0' }, fields_added: ['new_field'], fields_removed: [], fields_changed: ['order_id'] },
        affected_mappings: [{ source_field: 'order_id', target_field: 'orderId', reason: '字段定义发生变化，需要重新核对该映射' }],
      }
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('diff-section')).toBeTruthy())
    expect(screen.getByTestId('diff-section').textContent).toContain('1.0 → 2.0')
    expect(screen.getByTestId('affected-mappings').textContent).toContain('order_id')
  })

  it('delivered 状态可以导出回执并下载产物', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      if (typeof path === 'string' && path.includes('/export')) {
        return { receipt: { task_id: 'ad-task-0001' }, text: '适配回执文本', artifacts: [{ name: 'adaptation-exec-1.patch', size: 2048 }] }
      }
      return baseTask
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByText('导出回执')).toBeTruthy())
    screen.getByText('导出回执').click()
    await waitFor(() => expect(screen.getByTestId('export-section')).toBeTruthy())
    expect(screen.getByText(/适配回执文本/)).toBeTruthy()
    expect(screen.getByText(/adaptation-exec-1\.patch/)).toBeTruthy()
  })

  it('没有后继修订时提供"导入下一版契约"入口', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return baseTask
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByText('导入下一版契约')).toBeTruthy())
  })

  it('有后继修订时不提供"导入下一版契约"入口', async () => {
    api.mockImplementation(async (path) => {
      if (typeof path === 'string' && path.includes('/events')) return { events: [] }
      return { ...baseTask, successor_id: 'ad-task-0002' }
    })
    renderDetail('ad-task-0001')
    await waitFor(() => expect(screen.getByTestId('status-section')).toBeTruthy())
    expect(screen.queryByText('导入下一版契约')).toBeNull()
  })

  it('API 错误显示真实错误而不是假数据', async () => {
    api.mockImplementation(async () => { throw new Error('任务不存在') })
    renderDetail('ad-task-nonexistent')
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/任务不存在/)).toBeTruthy()
  })
})

describe('等待批准的计划', () => {
  it('有待批准计划时展示它并提供批准按钮', async () => {
    const withPlan = {
      ...baseTask,
      status: 'waiting',
      pending_plan: {
        revision: 1, title: '上报报文：补上鉴权头', summary: '适配器缺少 Authorization 头',
        questions: [],
        tasks: [{ id: 'auth', title: '补鉴权头并联调一次', paths: ['adapter.py'], checks: ['contract'], risk: 'low' }],
      },
    }
    api.mockImplementation(async (path, options) => {
      if (String(path).endsWith('/events')) return { events: [] }
      if (options?.method === 'POST') return { ...withPlan, status: 'running', pending_plan: null }
      return withPlan
    })
    renderDetail(baseTask.task_id)
    await waitFor(() => expect(screen.getByTestId('pending-plan')).toBeTruthy())
    expect(screen.getByTestId('pending-plan').textContent).toContain('adapter.py')
    const approve = screen.getByRole('button', { name: /批准计划/ })
    await act(async () => { fireEvent.click(approve) })
    await waitFor(() => expect(
      api.mock.calls.some(c => String(c[0]).endsWith('/approve') && c[1]?.method === 'POST')
    ).toBe(true))
  })

  it('没有待批准计划时不摆批准按钮', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return { ...baseTask, pending_plan: null }
    })
    renderDetail(baseTask.task_id)
    await waitFor(() => expect(screen.getByText(/接口适配/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /批准计划/ })).toBeNull()
  })
})


describe('模型提问时的回答入口', () => {
  it('展示服务端给的问题，回答后用读回的真实状态刷新，刷新后未回答的问题仍在', async () => {
    const waiting = {
      ...baseTask, status: 'waiting', pending_questions: ['金额是按含税还是不含税口径？'],
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

    renderDetail(baseTask.task_id)
    await waitFor(() => expect(screen.getByTestId('clarification-panel')).toBeTruthy())
    expect(screen.getByTestId('clarification-questions').textContent).toContain('金额是按含税还是不含税口径？')

    fireEvent.change(screen.getByTestId('clarification-answer'), { target: { value: '按不含税口径' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })

    await waitFor(() => expect(screen.queryByTestId('clarification-panel')).toBeNull())
    const posted = api.mock.calls.find(c => String(c[0]).endsWith('/clarify'))
    expect(posted).toBeTruthy()
    expect(String(posted![0])).toContain('/api/v2/adaptation/tasks/')
    expect(posted![1]?.body).toEqual({ answer: '按不含税口径' })
  })

  it('没有待回答的问题时不出现这一块', async () => {
    api.mockImplementation(async (path) => {
      if (String(path).endsWith('/events')) return { events: [] }
      return { ...baseTask, pending_questions: [] }
    })
    renderDetail(baseTask.task_id)
    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(screen.queryByTestId('clarification-panel')).toBeNull()
  })
})
