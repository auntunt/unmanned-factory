// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import HistoryPage, { historyGroup } from './HistoryPage'
import StartChat from './StartChat'
import RunWorkspace from './RunWorkspace'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const props = { csrfToken: 'test', onUnauthorized: vi.fn(), user: { id: 1, username: 'operator', role: 'admin' as const } }
const run = (id: string, status: string, extras = {}) => ({ id, project_id: 'enterprise-project', request: '目标 ' + id, status, revision: 1, updated_at: '2026-09-30T01:00:00Z', created_at: '2026-09-29T01:00:00Z', ...extras } as Run)
function Location() { const l = useLocation(); return <output data-testid="location">{l.search}</output> }
function history(entry = '/history') { return render(<MemoryRouter initialEntries={[entry]}><HistoryPage {...props} pollMs={0} /><Location /></MemoryRouter>) }
beforeEach(() => { api.mockReset(); sessionStorage.clear() })
afterEach(() => { cleanup(); vi.useRealTimers() })

it('groups actionable failures separately from completed, cancelled and superseded records', () => {
  expect(historyGroup(run('1', 'interrupted'))).toBe('attention')
  expect(historyGroup(run('2', 'failed'))).toBe('attention')
  expect(historyGroup(run('3', 'failed', { retry_run_id: '4' }))).toBe('done')
  expect(historyGroup(run('4', 'cancelled'))).toBe('done')
  expect(historyGroup(run('5', 'future-status'))).toBe('unknown')
})
it('restores search and status from URL, and clears both without losing unrelated parameters', async () => {
  api.mockResolvedValue({ runs: [run('blocked', 'needs_human'), run('active', 'running')] } as never)
  history('/history?status=attention&q=blocked&view=team')
  await screen.findByText('目标 blocked')
  expect(screen.queryByText('目标 active')).toBeNull()
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'missing' } })
  await screen.findByText('没有符合条件的任务')
  fireEvent.click(screen.getByText('清除筛选'))
  expect(screen.getByText('目标 active')).toBeTruthy()
  expect(screen.getByTestId('location').textContent).toBe('?view=team')
})
it('retains visible records when refresh fails and recovers on explicit retry', async () => {
  api.mockResolvedValueOnce({ runs: [run('old', 'running')] } as never)
    .mockRejectedValueOnce(new Error('网络暂不可用'))
    .mockResolvedValueOnce({ runs: [run('new', 'ready_for_review')] } as never)
  history()
  await screen.findByText('目标 old')
  fireEvent.click(screen.getByText('刷新列表'))
  await screen.findByText('同步失败，以下为上次成功读取的记录')
  expect(screen.getByText('目标 old')).toBeTruthy()
  fireEvent.click(screen.getByText('重新读取'))
  await screen.findByText('目标 new')
  expect(screen.queryByRole('alert')).toBeNull()
})
it('offers recovery for initial read failure and an actionable recent-task empty state', async () => {
  api.mockRejectedValueOnce(new Error('断开连接')).mockResolvedValueOnce({ runs: [] } as never)
  history()
  await screen.findByText('任务列表暂时不可用')
  fireEvent.click(screen.getByText('重新读取'))
  expect((await screen.findByText('创建新任务')).closest('a')?.getAttribute('href')).toBe('/')
})
it('aborts an in-flight request on navigation away', () => {
  api.mockImplementation(() => new Promise(() => {}))
  const page = history()
  const signal = api.mock.calls[0][1]?.signal
  page.unmount()
  expect(signal?.aborted).toBe(true)
})
it('keeps refresh disabled while a request is pending', async () => {
  let resolve: (v: unknown) => void = () => {}
  api.mockImplementation(() => new Promise(r => { resolve = r }) as never)
  history()
  expect((screen.getByText('正在刷新…') as HTMLButtonElement).disabled).toBe(true)
  await act(async () => resolve({ runs: [] }))
  expect((screen.getByText('刷新列表') as HTMLButtonElement).disabled).toBe(false)
})
it('shows real setup and project entry links without creating anything', () => {
  render(<MemoryRouter><StartChat {...props} /></MemoryRouter>)
  expect(screen.getByText('检查运行配置').closest('a')?.getAttribute('href')).toBe('/settings/runtime')
  expect(screen.getByText('继续现有项目').closest('a')?.getAttribute('href')).toBe('/projects')
  expect(api).not.toHaveBeenCalled()
})
it('attachment keyboard activation does not submit the task', () => {
  const { container } = render(<MemoryRouter><StartChat {...props} /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('需求'), { target: { value: '企业工单系统' } })
  const picker = container.querySelector<HTMLInputElement>('input[type=file]')!
  const click = vi.spyOn(picker, 'click').mockImplementation(() => {})
  fireEvent.keyDown(picker.closest('label')!, { key: 'Enter' })
  expect(click).toHaveBeenCalledOnce()
  expect(api).not.toHaveBeenCalled()
})
it('members see assigned-project entry instead of administrator creation controls', () => {
  render(<MemoryRouter><StartChat {...props} user={{ ...props.user, role: 'member' }} /></MemoryRouter>)
  expect(screen.getByText('我的作品').closest('a')?.getAttribute('href')).toBe('/projects')
  expect(screen.queryByLabelText('需求')).toBeNull()
})
it('disables approval controls for non-owning members while retaining task context', async () => {
  api.mockImplementation(async url => url.endsWith('/conversation') ? { messages: [] } as never : run('r1', 'awaiting_approval', { source: { actor_id: 2 } }) as never)
  render(<MemoryRouter initialEntries={['/runs/r1']}><Routes><Route path="/runs/:runId" element={<RunWorkspace {...props} user={{ ...props.user, role: 'member' }} pollMs={0} />} /></Routes></MemoryRouter>)
  await screen.findByRole('heading', { name: '需求 #r1' })
  expect((screen.getByRole('button', { name: '批准方案' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('项目 #enterprise-project').closest('a')?.getAttribute('href')).toBe('/projects/enterprise-project')
  fireEvent.click(screen.getByRole('button', { name: '批准方案' }))
  await waitFor(() => expect(api.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0))
})

it('does not interpret the literal search query all as clearing the filter', async () => {
  api.mockResolvedValue({ runs: [run('all-target', 'running'), run('other', 'running')] } as never)
  history()
  await screen.findByText('目标 all-target')
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'all' } })
  expect(screen.queryByText('目标 other')).toBeNull()
  expect(screen.getByTestId('location').textContent).toBe('?q=all')
})

it('refreshes visible pages, skips overlapping reads, and stops polling on unmount', async () => {
  vi.useFakeTimers()
  api.mockResolvedValue({ runs: [run('poll', 'running')] } as never)
  const page = render(<MemoryRouter><HistoryPage {...props} pollMs={1000} /></MemoryRouter>)
  await act(async () => {})
  expect(api).toHaveBeenCalledTimes(1)
  await act(async () => { vi.advanceTimersByTime(1000) })
  expect(api).toHaveBeenCalledTimes(2)
  api.mockImplementation(() => new Promise(() => {}))
  await act(async () => { vi.advanceTimersByTime(1000) })
  expect(api).toHaveBeenCalledTimes(3)
  await act(async () => { vi.advanceTimersByTime(5000) })
  expect(api).toHaveBeenCalledTimes(3)
  page.unmount()
  await act(async () => { vi.advanceTimersByTime(5000) })
  expect(api).toHaveBeenCalledTimes(3)
})

it('shows acceptance-ledger counters only from an explicit structured ledger', async () => {
  api.mockImplementation(async url => {
    if (url.endsWith('/conversation')) return { messages: [] } as never
    if (url.endsWith('/deliverables')) return { items: [] } as never
    return run('ledger', 'ready_for_review', { artifacts: { acceptance_ledger: {
      counts: { pass: 2, fail: 1, unverified: 3 },
      items: [{ id: 'criterion', text: '已记录的验收条件', status: 'fail' }],
    } } }) as never
  })
  render(<MemoryRouter initialEntries={['/runs/ledger']}><Routes><Route path="/runs/:runId" element={<RunWorkspace {...props} pollMs={0} />} /></Routes></MemoryRouter>)
  await screen.findByText('查看验证记录')
  expect(screen.getByText('通过 2')).toBeTruthy()
  expect(screen.getByText('未通过 1')).toBeTruthy()
  expect(screen.getByText('未验证 3')).toBeTruthy()
  expect(screen.getByText('已记录的验收条件')).toBeTruthy()
})
