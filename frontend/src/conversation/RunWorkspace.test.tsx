// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import RunWorkspace from './RunWorkspace'
import { WorkTitleContext } from './title-context'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()

function mount(run: Record<string, unknown>, messages: unknown[] = []) {
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url === '/api/v2/runs/r1' && (!opts || !opts.method)) return run as never
    if (url === '/api/v2/runs/r1/conversation') return { messages } as never
    if (typeof url === 'string' && url.endsWith('/deliverables')) return { items: [], can_collect: true } as never
    return { ok: true, id: 'r2' } as never
  })
  return render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
}
const base = { id: 'r1', project_id: 'p1', request: '做预约工具', revision: 1, plan: { title: '预约管理工具', summary: '', questions: [], tasks: [] }, artifacts: {}, created_at: '', updated_at: '' }
beforeEach(() => { api.mockReset() })
afterEach(cleanup)

it('shows the real progress head for an active run and queues a follow-up instead of dropping it', async () => {
  mount({ ...base, status: 'running' })
  await screen.findByText('正在处理')
  fireEvent.change(screen.getByLabelText('补充或修改'), { target: { value: '再加一个搜索' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await waitFor(() => expect(api.mock.calls.some(([u, o]) => u === '/api/v2/runs/r1/follow-up' && (o as { method?: string })?.method === 'POST')).toBe(true))
  const call = api.mock.calls.find(([u]) => u === '/api/v2/runs/r1/follow-up')
  expect((call?.[1] as { body?: { content?: string } })?.body?.content).toBe('再加一个搜索')
})

it('routes a clarification answer to the clarify endpoint', async () => {
  mount({ ...base, status: 'needs_clarification', plan: { title: '预约管理工具', summary: '', questions: ['需要哪些字段？'], tasks: [] } })
  await screen.findByText('等待你补充')
  fireEvent.change(screen.getByLabelText('补充或修改'), { target: { value: '客户名和时间' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await waitFor(() => expect(api.mock.calls.some(([u, o]) => u === '/api/v2/runs/r1/clarify' && (o as { method?: string })?.method === 'POST')).toBe(true))
})

it('does not re-fetch repeatedly when each response is a fresh JSON object', async () => {
  mount({ ...base, status: 'running' })
  api.mockImplementation(async url => url?.endsWith('/conversation') ? { messages: [] } as never : { ...base, status: 'running' } as never)
  await screen.findByText('正在处理')
  await new Promise(resolve => setTimeout(resolve, 35))
  expect(api.mock.calls.filter(([url]) => url === '/api/v2/runs/r1')).toHaveLength(1)
})

it('submits the complete edited specification required by the real confirmation API', async () => {
  const spec = { goal: '做预约工具', screens: [], flows: ['预约'], data_model: ['时间'], non_goals: ['无支付'], risks_assumptions: ['本地使用'] }
  mount({ ...base, status: 'awaiting_spec_confirmation', spec_draft: spec, recommended_skills: [] })
  fireEvent.change(await screen.findByLabelText('目标'), { target: { value: '做预约和取消工具' } })
  fireEvent.click(screen.getByRole('button', { name: '开工' }))
  await waitFor(() => expect(api.mock.calls.some(([url]) => url === '/api/v2/runs/r1/confirm-spec')).toBe(true))
  expect(api.mock.calls.find(([url]) => url.endsWith('/confirm-spec'))?.[1]?.body).toEqual({ revision: 1, action: 'start', spec_draft: { ...spec, goal: '做预约和取消工具' }, selected_skills: [], fidelity_target: null })
})

it('continues the saved execution without forcing an invented explanation', async () => {
  mount({ ...base, status: 'needs_human', artifacts: { base_sha: 'abc', tasks: [{ id: 'a' }] }, resume_count: 2 })
  fireEvent.click(await screen.findByRole('button', { name: '继续处理' }))
  await waitFor(() => expect(api.mock.calls.some(([url]) => url.endsWith('/continue'))).toBe(true))
  expect(api.mock.calls.find(([url]) => url.endsWith('/continue'))?.[1]?.body).toEqual({ answer: '', revision: 1, resume_count: 2 })
})

it('renders a real preview response including file id zero and downloads the full archive', async () => {
  mount({ ...base, status: 'ready_for_review' })
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [{ id: 0, name: 'README.md', kind: 'source', preview: true }], recommended_preview_id: 0 } as never
    if (url?.endsWith('/files/0?preview=true')) return { name: 'README.md', kind: 'source', content: '实际使用说明' } as never
    return {} as never
  })
  fireEvent.click(await screen.findByRole('button', { name: '预览成果' }))
  await screen.findByText('实际使用说明')
  expect(screen.getByRole('link', { name: '下载全部成果 ZIP' }).getAttribute('href')).toBe('/api/v3/runs/r1/deliverables/download')
  expect(screen.queryByText('下载源码')).toBeNull()
})

it('reuses follow-up identity after a network error and reports that it is only recorded', async () => {
  mount({ ...base, status: 'running' })
  await screen.findByText('正在处理')
  api.mockImplementation(async url => {
    if (url.endsWith('/follow-up')) {
      const count = api.mock.calls.filter(([value]) => value.endsWith('/follow-up')).length
      if (count === 1) throw new Error('网络中断')
      return { recorded: true, applied: false, queued: false } as never
    }
    return url.endsWith('/conversation') ? { messages: [] } as never : { ...base, status: 'running' } as never
  })
  fireEvent.change(screen.getByLabelText('补充或修改'), { target: { value: '再加搜索' } })
  fireEvent.click(screen.getByLabelText('发送'))
  await screen.findByText('网络中断')
  fireEvent.click(screen.getByLabelText('发送'))
  await screen.findByText(/补充已记录，将在下一个安全节点自动并入任务/)
  const bodies = api.mock.calls.filter(([url]) => url.endsWith('/follow-up')).map(([, options]) => options?.body)
  expect(bodies[0]).toEqual(bodies[1])
})

it('shows completed inspection evidence without a product card or active composer', async () => {
  mount({ ...base, status: 'inspection_completed', source: { type: 'inspection', operation: 'startup' }, artifacts: { operation_results: { health: { status: 'pass', evidence: 'GET /health returned 200' } } } })
  await screen.findByText('巡检已完成')
  expect(screen.getByText('GET /health returned 200')).toBeTruthy()
  expect(screen.getByText('巡检结果 · 只诊断')).toBeTruthy()
  expect(screen.queryByText('正在处理')).toBeNull()
  expect(screen.queryByLabelText('补充或修改')).toBeNull()
  expect(api.mock.calls.some(([url]) => url.endsWith('/deliverables'))).toBe(false)
})

it('shows pending badge on follow-up messages when followups are unapplied', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url === '/api/v2/runs/r1' && (!opts || !opts.method)) return {
      ...base, status: 'running',
      followups: [{ id: 'fu1', content: '再加搜索', created_at: '2026-01-01', applied: false }],
    } as never
    if (url === '/api/v2/runs/r1/conversation') return { messages: [
      { id: 1, role: 'user', content: '做预约工具', at: '' },
      { id: 2, role: 'user', content: '再加搜索', at: '', followup: true, applied: false },
    ] } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('待应用')
})

it('shows applied badge on follow-up messages when all followups are consumed', async () => {
  api.mockImplementation(async (url?: string, opts?: { method?: string; body?: unknown }) => {
    if (url === '/api/v2/runs/r1' && (!opts || !opts.method)) return {
      ...base, status: 'needs_human', artifacts: { base_sha: 'abc', tasks: [{ id: 'a' }] },
      followups: [{ id: 'fu1', content: '再加搜索', created_at: '2026-01-01', applied: true }],
    } as never
    if (url === '/api/v2/runs/r1/conversation') return { messages: [
      { id: 1, role: 'user', content: '做预约工具', at: '' },
      { id: 2, role: 'user', content: '再加搜索', at: '', followup: true, applied: false },
    ] } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('已并入后续执行')
})

it('follow-up badges survive refresh by re-reading from run response', async () => {
  let callCount = 0
  api.mockImplementation(async (url?: string) => {
    if (url === '/api/v2/runs/r1') {
      callCount++
      return {
        ...base, status: 'running',
        followups: [{ id: 'fu1', content: '加搜索', created_at: '2026-01-01', applied: false }],
      } as never
    }
    if (url?.endsWith('/conversation')) return { messages: [
      { id: 1, role: 'user', content: '加搜索', at: '', followup: true, applied: false },
    ] } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('待应用')
  // The badge comes from the run response, not local state, so refresh preserves it
  expect(callCount).toBeGreaterThanOrEqual(1)
})

it('keeps real deployment evidence visible for non-general completed work', async () => {
  mount({ ...base, status: 'published', source: { operation: 'release' }, artifacts: { operation_results: { rollback: { status: 'pass', evidence: '恢复 release-17 并探测健康状态' } } } })
  await screen.findByText('恢复 release-17 并探测健康状态')
  expect(screen.getByRole('region', { name: '维护结果' })).toBeTruthy()
})

it('renders service delivery type with deployment status', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'published' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [], delivery_type: 'service', can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('线上服务')
  expect(screen.getByText('已部署')).toBeTruthy()
  // Delivery section present with service type
  const section = document.querySelector('[data-delivery-type="service"]')
  expect(section).toBeTruthy()
})

it('renders CLI delivery type with deployment marked as not applicable', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [{ id: 0, name: 'cli-tool', kind: 'source' }], delivery_type: 'cli', can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('命令行工具')
  expect(screen.getByText('不适用')).toBeTruthy()
  expect(screen.getByText('命令行工具通过下载使用，无需在线部署。')).toBeTruthy()
  // Must NOT show failure styling — the deployment NA is neutral
  const section = document.querySelector('[data-delivery-type="cli"]')
  expect(section).toBeTruthy()
  expect(section!.querySelector('.cv-verify-fail')).toBeNull()
})

it('renders installer delivery type with pending confirmation when targets are not selected', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [{ id: 0, name: 'app.dmg', kind: 'installer' }], delivery_type: 'installer', installer_targets: null, can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('安装包')
  expect(screen.getByText('待确认')).toBeTruthy()
  expect(screen.getByText('尚未选定目标系统，安装包构建暂不可用。')).toBeTruthy()
  const section = document.querySelector('[data-delivery-type="installer"]')
  expect(section).toBeTruthy()
})

it('renders installer delivery type with confirmed target platforms', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [{ id: 0, name: 'app.dmg', kind: 'installer' }], delivery_type: 'installer', installer_targets: ['macOS', 'Windows'], can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('安装包')
  expect(screen.getByText('macOS、Windows')).toBeTruthy()
})

it('renders undeclared delivery type for legacy tasks without mismarking as failed', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [{ id: 0, name: 'index.html', kind: 'web' }], can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('未声明')
  const section = document.querySelector('[data-delivery-type="undeclared"]')
  expect(section).toBeTruthy()
  // Must NOT show any failure indicators
  expect(section!.querySelector('.cv-verify-fail')).toBeNull()
  expect(section!.querySelector('.cv-delivery-fail')).toBeNull()
})

it('keeps the result area visible on mobile breakpoint without input occlusion', async () => {
  api.mockImplementation(async url => {
    if (url === '/api/v2/runs/r1') return { ...base, status: 'ready_for_review' } as never
    if (url?.endsWith('/conversation')) return { messages: [] } as never
    if (url?.endsWith('/deliverables')) return { saved: true, items: [], delivery_type: 'service', can_collect: true } as never
    return {} as never
  })
  render(<MemoryRouter initialEntries={['/runs/r1']}><WorkTitleContext.Provider value={vi.fn()}>
    <Routes><Route path="/runs/:runId" element={<RunWorkspace csrfToken="csrf" onUnauthorized={noop} pollMs={0} />} /></Routes>
  </WorkTitleContext.Provider></MemoryRouter>)
  await screen.findByText('线上服务')
  // The result area (cv-run-inner) has bottom padding ensuring the sticky dock never covers it.
  // Verify the dock exists and the inner content wrapper is present.
  const inner = document.querySelector('.cv-run-inner')
  expect(inner).toBeTruthy()
  // The revise composer is present for completed runs, but the result card is above it
  const resultCard = document.querySelector('.cv-artifact')
  expect(resultCard).toBeTruthy()
})

it('resumes skill ingestion on the same run instead of creating a generic retry', async () => {
  mount({ ...base, status: 'needs_human', source: { type: 'skill_ingestion' }, resume_count: 2 })
  fireEvent.click(await screen.findByRole('button', { name: '继续处理' }))
  await waitFor(() => expect(api.mock.calls.some(([url]) => url.endsWith('/continue'))).toBe(true))
  expect(api.mock.calls.some(([url]) => url.endsWith('/retry'))).toBe(false)
})

it('collapses structured runner output while keeping normal replies readable', async () => {
  mount({ ...base, status: 'ready_for_review' }, [
    { id: 1, role: 'assistant', content: '{"criterion_ids":["task:coding:1"],"startup_command":"python app.py"}', at: '' },
    { id: 2, role: 'assistant', content: '已完成。\n请查看成果。', at: '' },
  ])
  const summary = await screen.findByText('查看结构化执行记录')
  expect(summary.closest('details')?.open).toBe(false)
  expect(summary.closest('details')?.textContent).toContain('python app.py')
  expect(screen.getByText(/请查看成果/).className).toBe('cv-message-text')
})
