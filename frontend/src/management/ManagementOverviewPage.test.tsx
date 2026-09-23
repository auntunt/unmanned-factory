// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ManagementOverviewPage from './ManagementOverviewPage'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

function overview(overrides: Record<string, unknown> = {}) {
  return {
    scope: { unit_id: null, label: '全部组织与未归属项目', admin: true, units: [], grants: [] },
    window: { days: 30, since: '2026-08-01T00:00:00Z', note: '' },
    generated_at: '2026-09-23T00:00:00Z',
    counts: { in_progress: 1, pending: 1, ready: 0, published: 0, failed: 0 },
    projects: [],
    pending: [],
    usage: { window_days: 30, runs_considered: 0, recorded: false, known_cost_usd: null, unknown_cost_calls: 0, calls: 0,
              cost_source: '运行事件 usage.recorded', tokens: { month: '2026-09', settled_tokens: 0, unknown_calls: 0, reserved_calls: 0, source: 'token_calls', recorded: false } },
    audit: [],
    notes: { published: '“已发布”不等于已上线。', responsibility: '', execution: '管理查看不包含执行、取消、批准或读取对话的权限。' },
    ...overrides,
  }
}

function mount() {
  return render(<MemoryRouter initialEntries={['/management']}><ManagementOverviewPage csrfToken="x" onUnauthorized={noop} /></MemoryRouter>)
}

beforeEach(() => { api.mockReset() })
afterEach(cleanup)

it('never shows $0 when usage is not recorded', async () => {
  api.mockResolvedValue(overview() as never)
  mount()
  const notices = await screen.findAllByText('未记录')
  expect(notices.length).toBeGreaterThanOrEqual(2) // known cost + tokens, both unrecorded
  expect(screen.queryByText('$0.00')).toBeNull()
  expect(screen.queryByText(/^\$0/)).toBeNull()
})

it('shows another-N-calls-unknown note when unknown_cost_calls > 0, still never $0', async () => {
  api.mockResolvedValue(overview({
    usage: { window_days: 30, runs_considered: 2, recorded: true, known_cost_usd: 1.5, unknown_cost_calls: 3, calls: 5,
             cost_source: '运行事件 usage.recorded', tokens: { month: '2026-09', settled_tokens: 100, unknown_calls: 0, reserved_calls: 0, source: 'token_calls', recorded: true } },
  }) as never)
  mount()
  await screen.findByText('$1.50')
  expect(screen.getByText('另有 3 次调用费用未知')).toBeTruthy()
})

it('shows a plain action hint (no button) when a pending task cannot be acted on', async () => {
  api.mockResolvedValue(overview({
    pending: [{ run_id: 'r1', project_id: 'p1', project_name: '示例项目', title: '示例任务', status: 'awaiting_approval',
                reason: '等待批准执行方案', initiator: '张三', updated_at: '2026-09-20T00:00:00Z', can_act: false,
                action_hint: '需要发起人或管理员处理；管理查看不包含执行或批准权限' }],
  }) as never)
  mount()
  await screen.findByText('示例任务')
  expect(screen.getByText('需要发起人或管理员处理；管理查看不包含执行或批准权限')).toBeTruthy()
  expect(screen.queryByText('去工作台处理 →')).toBeNull()
  expect(screen.queryByRole('link', { name: /批准/ })).toBeNull()
})

it('links to the workbench run when can_act is true', async () => {
  api.mockResolvedValue(overview({
    pending: [{ run_id: 'r2', project_id: 'p1', project_name: '示例项目', title: '示例任务2', status: 'needs_clarification',
                reason: '等待补充信息', initiator: '李四', updated_at: '2026-09-20T00:00:00Z', can_act: true,
                action_hint: '你是发起人，可在工作台处理' }],
  }) as never)
  mount()
  await screen.findByText('示例任务2')
  const link = screen.getByText('去工作台处理 →') as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/runs/r2')
})
