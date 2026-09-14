// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { request } from '../workspace/api'
import { CopyValue, LoadingCard, runTitle } from './presentation'
import ProjectLifecycle from './ProjectLifecycle'
import { PROJECT_STAGES } from './project-stages'
import { runGuidance } from './run-guidance'
import { InspectionTrend } from './ProjectInspection'
import RunActions from './RunActions'
import RunJourney from './RunJourney'
import { StopReason } from './StopReason'
import OverviewPage from './OverviewPage'
import RunsPage from './RunsPage'
import RunPage from './RunPage'
import RuntimePage from './RuntimePage'
import CapabilitiesPage from './CapabilitiesPage'
import CostsPage from './CostsPage'
import { ProjectTargets } from './ServerTargets'
import VerifiedOperationsFacts from './VerifiedOperationsFacts'
import Workbench from './Workbench'
import AttentionList from './AttentionList'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'owner', role: 'admin' as const } }
const run: Run = { id: '1234567890abcdef1234567890abcdef', project_id: 'abcdef1234567890abcdef1234567890', request: '修复工时导出\n兼容已有规则', status: 'needs_human', execution_mode: 'continuous', revision: 1, created_at: '2026-09-14T01:00:00Z', updated_at: '2026-09-14T02:00:00Z', plan: { title: '持续编码', summary: '', tasks: [], questions: [] }, artifacts: {} }
const stages = PROJECT_STAGES.map(stage => ({ ...stage, count: 1, unit: '条记录', items: [{ id: String(run.id), title: '持续编码', status: run.status, updated_at: run.updated_at, href: `/runs/${run.id}` }] }))
const engineering = { stages, verified_runs: 1, published_runs: 0, distilled_runs: 0, reused_runs: 0, draft_capabilities: 0, ready_capabilities: 0 }
const project = { id: String(run.project_id), name: '工时项目', repository: 'local/hours', next_run: run, active_runs: 1, attention_runs: 1, engineering }
const overview = { projects: 1, runs: 1, active_runs: 1, attention_runs: 1, engineering, run_snapshots: [run], project_summaries: [project], attention: [], recent_events: [], model_usage: [] }
function show(node: React.ReactNode) { return render(<MemoryRouter>{node}</MemoryRouter>) }
beforeEach(() => { api.mockReset(); api.mockImplementation(async url => {
  if (url === '/api/v3/overview') return overview as never
  if (url === '/api/v2/runs') return { runs: [run] } as never
  if (url === '/api/v2/projects') return { projects: [project] } as never
  if (url === '/api/v3/capabilities') return { capabilities: [] } as never
  if (url.endsWith('/knowledge')) return { entries: [] } as never
  if (url.includes('/deploy-targets')) return { targets: [], available: [], revision: 0 } as never
  return {} as never
}); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } }) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('prefers request first line, then plan, then short id; caps at 40 characters', () => {
  expect(runTitle(run)).toBe('修复工时导出')
  expect(runTitle({ ...run, request: '', plan: { ...run.plan!, title: '整理代码' } })).toBe('整理代码')
  expect(runTitle({ ...run, request: '' })).toBe('运行 12345678')
  expect(runTitle({ ...run, request: '长'.repeat(60) })).toHaveLength(40)
})
it('copies the full value while rendering only the short number and reports failure', async () => {
  show(<CopyValue value={run.id} label="运行编号" />)
  expect(screen.queryByText(String(run.id))).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '复制完整运行编号' }))
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(run.id)
  await screen.findByText('已复制')
  vi.mocked(navigator.clipboard.writeText).mockRejectedValueOnce(new Error('denied'))
  fireEvent.click(screen.getByRole('button', { name: '复制完整运行编号' }))
  await screen.findByText('复制失败，请重试')
})
it('renders six stage tabs and full-width details with one boundary note, keyboard selection and short delivery metadata', () => {
  const onSelect = vi.fn()
  const delivery = { ...run, artifacts: { commit: 'a'.repeat(40), branch: `factory/${run.id}` } }
  const { container } = show(<ProjectLifecycle projectId={project.id} projectName={project.name} engineering={engineering} runs={[delivery]} selectedStage="deliver" onSelect={onSelect} requirementForm={null} isAdmin />)
  const tabs = screen.getAllByRole('tab')
  expect(tabs).toHaveLength(6)
  expect(screen.getAllByText(/数量不代表完成率/)).toHaveLength(1)
  expect(screen.getByRole('tabpanel').id).toBe('ov3-stage-details')
  expect(container.querySelector('.el-loop')).toBeNull()
  expect(screen.getByRole('button', { name: '复制完整提交 SHA' }).textContent).toContain('aaaaaaa')
  expect(screen.queryByText(`factory/${run.id}`)).toBeNull()
  tabs[4].focus(); fireEvent.keyDown(tabs[4], { key: 'ArrowLeft' })
  expect(onSelect).toHaveBeenCalledWith('verify')
})
it('keeps short guidance labels within eight characters and full summary only once in the run journey', () => {
  for (const status of ['needs_human', 'awaiting_approval', 'ready_for_review', 'running', 'failed'] as const) expect(Array.from(runGuidance({ ...run, status }).label).length).toBeLessThanOrEqual(8)
  show(<><StopReason run={run} canConfigure /><RunJourney run={run} activeView="execution" /></>)
  expect(screen.getAllByText(runGuidance(run).summary)).toHaveLength(1)
  expect(screen.getAllByRole('tablist', { name: '运行工作视图' })).toHaveLength(1)
  expect(screen.getByRole('tab', { name: /开发执行/ }).getAttribute('aria-selected')).toBe('true')
})
it('limits trend to latest 20, gives every cell Chinese result and duration, and flags persistent failures', () => {
  const history = Array.from({ length: 22 }, (_, i) => ({ run_id: `r${i}`, at: new Date(Date.UTC(2026, 8, 14, i)).toISOString(), verdict: 'pass' as const, duration_s: 3.2 }))
  show(<InspectionTrend history={history} consecutiveFailures={3} />)
  expect(screen.getAllByRole('link')).toHaveLength(20)
  for (const link of screen.getAllByRole('link')) { expect(link.getAttribute('aria-label')).toMatch(/通过 · 3.2s/); expect(link.title).toBe(link.getAttribute('aria-label')) }
  expect(screen.getByText('持续故障')).toBeTruthy()
})
it('places destructive actions in overflow and requires confirmation with focus return', () => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open') } })
  const onCancel = vi.fn()
  show(<RunActions run={run} canCancel canClarify canDiscard busy={false} onCancel={onCancel} onDiscard={vi.fn()} />)
  expect(document.querySelectorAll('.wb-detail-actions > .wb-button-primary')).toHaveLength(1)
  const menu = screen.getByLabelText('更多运行操作').parentElement as HTMLDetailsElement
  expect(menu.open).toBe(false); fireEvent.click(menu.querySelector('summary')!)
  const cancel = screen.getByRole('button', { name: '取消运行' }); fireEvent.click(cancel)
  expect(onCancel).not.toHaveBeenCalled()
  const keep = screen.getByRole('button', { name: '保留运行' }); expect(document.activeElement).toBe(keep)
  fireEvent.click(keep); expect(document.activeElement).toBe(cancel)
  fireEvent.click(cancel); fireEvent.click(screen.getByRole('button', { name: '确认取消' })); expect(onCancel).toHaveBeenCalledOnce()
})
it('gives returning users compact entry and shared stage strip', async () => {
  show(<OverviewPage {...props} />)
  await screen.findByText('工时项目')
  expect(screen.queryByText('开始一项新工作')).toBeNull()
  expect(screen.getAllByRole('navigation', { name: '工程阶段' })).toHaveLength(1)
  expect(screen.queryByText('计费方式')).toBeNull()
})
it('keeps new-user large entry and adds publication metadata to recent deliverables', async () => {
  api.mockResolvedValueOnce({ ...overview, projects: 0, project_summaries: [] } as never)
  const view = show(<OverviewPage {...props} />); await screen.findByText('开始一项新工作'); view.unmount()
  api.mockResolvedValueOnce({ ...overview, project_summaries: [{ ...project, active_runs: 0, attention_runs: 0, next_run: { ...run, status: 'ready_for_review' } }] } as never)
  show(<OverviewPage {...props} />)
  const card = await screen.findByRole('region', { name: '最近成果' })
  expect(within(card).getByText(/未发布/)).toBeTruthy(); expect(within(card).getByText('已验证')).toBeTruthy()
})
it('shows request titles and short copy ids in runs list without long repeated guidance', async () => {
  show(<RunsPage {...props} />)
  await screen.findByText('修复工时导出')
  expect(screen.queryByText(runGuidance(run).summary)).toBeNull()
  expect(screen.getByRole('button', { name: '复制完整运行编号' }).textContent).toContain('12345678')
  expect(screen.getByText('持续编码')).toBeTruthy()
})
it('merges run configuration sections in one disclosure and navigation in one tablist', async () => {
  api.mockResolvedValue({ ...run, status: 'running' } as never)
  render(<MemoryRouter initialEntries={[`/runs/${run.id}?view=requirements`]}><Routes><Route path="/runs/:runId" element={<RunPage {...props} user={{ ...props.user, role: 'member' }} />} /></Routes></MemoryRouter>)
  await screen.findByRole('heading', { name: /修复工时导出/, level: 1 })
  expect(screen.getByText(runGuidance({ ...run, status: 'running' }).summary).closest('.wb-run-journey')).toBeTruthy()
  const snapshot = screen.getByText('运行配置快照').closest('details')!
  expect(snapshot.open).toBe(false)
  expect(within(snapshot).getByText('已搭配能力')).toBeTruthy()
  expect(within(snapshot).getByText(/提交时定格/)).toBeTruthy()
  expect(screen.getAllByRole('tablist', { name: '运行工作视图' })).toHaveLength(1)
  expect(screen.getAllByText(runGuidance({ ...run, status: 'running' }).summary)).toHaveLength(1)
})
it('hides empty verified facts and shows truthful bound target health', async () => {
  const view = show(<VerifiedOperationsFacts projectId="p1" {...props} isAdmin />)
  await waitFor(() => expect(api).toHaveBeenCalled()); expect(view.container.textContent).toBe(''); view.unmount()
  api.mockResolvedValue({ targets: ['t1'], revision: 1, available: [{ id: 't1', name: '生产服务' }] } as never)
  show(<ProjectTargets projectId="p1" {...props} isAdmin={false} runs={[{ ...run, artifacts: { remote_results: [{ target_id: 't1', verb: 'health_check', status: 'pass' }] } }]} />)
  await screen.findByText(/生产服务/); expect(screen.getByText('通过')).toBeTruthy(); expect(screen.queryByRole('checkbox')).toBeNull()
})
it('shows unbound target setup link for admin and explanation for member', async () => {
  const view = show(<ProjectTargets projectId="p1" {...props} isAdmin />)
  await screen.findByRole('link', { name: '去运行配置绑定' }); view.unmount()
  show(<ProjectTargets projectId="p1" {...props} isAdmin={false} />)
  await screen.findByText('由管理员配置'); expect(screen.queryByRole('link')).toBeNull()
})
it('uses a labelled skeleton during runtime loading', () => {
  api.mockReturnValue(new Promise(() => {}))
  show(<RuntimePage {...props} />)
  expect(screen.getByRole('status', { name: '正在读取运行配置与环境诊断' }).querySelectorAll('span')).toHaveLength(3)
})
it('merges empty capability library into one actionable empty state', async () => {
  show(<CapabilitiesPage {...props} />)
  await screen.findByText('还没有工作能力')
  expect(screen.queryByText('先选一项工作能力')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '创建第一项能力' }))
  expect(screen.getByRole('button', { name: /关闭新建/ })).toBeTruthy()
})
it('collapses unknown usage into one badge and explains unknown cache on focus', async () => {
  api.mockResolvedValueOnce({ ...overview, model_usage: [{ profile: 'standard', provider: 'claude', model: 'opus', calls: 1, token_usage_calls: 0, cache_usage_calls: 0 }] } as never)
  show(<CostsPage {...props} />)
  await screen.findByText('供应商未返回明细')
  expect(screen.queryAllByText('未记录')).toHaveLength(0)
  expect(screen.getByText('未知').title).toContain('未返回')
})
it('keeps side navigation links named and loading status accessible', () => {
  show(<Workbench {...props} user={props.user} onLogout={vi.fn()}><LoadingCard label="测试加载" /></Workbench>)
  const nav = screen.getByRole('complementary', { name: '工作台导航' })
  for (const link of within(nav).getAllByRole('link')) expect(link.textContent?.trim()).not.toBe('')
  expect(screen.getByRole('status', { name: '测试加载' })).toBeTruthy()
})

it('attention rows use short guidance, request title and date without a long summary', () => {
  show(<AttentionList items={[{ id: String(run.id), run_snapshot: run, status: run.status, updated_at: run.updated_at, title: '持续编码' }]} />)
  expect(screen.getByText('修复工时导出')).toBeTruthy()
  expect(screen.getByText('已暂停·可重试')).toBeTruthy()
  expect(screen.queryByText(runGuidance(run).summary)).toBeNull()
  expect(screen.getByRole('link', { name: '查看记录' }).getAttribute('href')).toContain('#run-recovery')
})
