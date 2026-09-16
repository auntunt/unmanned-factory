// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { request } from '../workspace/api'
import type { Run, AuditEvent } from '../workspace/types'
import ThemeSwitch, { applyTheme, THEME_KEY } from './ThemeSwitch'
import ExecutionLog from './ExecutionLog'
import AttentionList from './AttentionList'
import RunsPage from './RunsPage'
import Workbench from './Workbench'
import ProjectsPage from './ProjectsPage'
import { ListTime, relativeTime } from './presentation'
import { LegacyCapabilityRedirect } from './capability-links'
import CapabilityCenter from './CapabilityCenter'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'owner', role: 'admin' as const } }
const run: Run = { id:'run-one', project_id:'p1', status:'needs_human', request:'修复 CSV', revision:1, created_at:'2026-09-14T00:00:00Z', updated_at:'2026-09-14T00:00:00Z' }
const project = { id:'p1', name:'工时项目', repository:'local/hours', base_branch:'main', checks:{} }
const module = { id:'m1', name:'维护方法', category:'workflow', version:1, instructions:'先复现再修复', description:'可靠维护' }
const capability = { id:'c1', name:'工时维护', revision:1, status:'ready', category:'engineering', description:'CSV', source_run_id:run.id, updated_at:run.updated_at, created_at:run.created_at, acceptance:[] }
function show(node: React.ReactNode, path = '/') { return render(<MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>) }
beforeEach(() => { localStorage.clear(); delete document.documentElement.dataset.theme; api.mockReset(); Element.prototype.scrollIntoView = vi.fn(); api.mockImplementation(async url => {
  if (url === '/api/v2/runs') return { runs: [{ ...run, module_snapshot:[module, module] }] } as never
  if (url === '/api/v2/projects') return { projects:[project] } as never
  if (url === '/api/v4/modules') return { modules:[module] } as never
  if (url === '/api/v3/capabilities') return { capabilities:[capability] } as never
  if (url === '/api/v3/capabilities/c1') return { ...capability, versions:[capability] } as never
  return {} as never
}) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
it('uses just one short status badge per attention/list row', async () => {
  const view = show(<AttentionList items={[{ id:String(run.id), status:run.status, updated_at:run.updated_at, run_snapshot:run }]} />)
  expect(view.container.querySelectorAll('.wb-status')).toHaveLength(1)
  expect(screen.getByText('已暂停·可重试')).toBeTruthy(); expect(screen.queryByText('运行已暂停')).toBeNull()
  view.unmount(); const list = show(<RunsPage {...props} />); await screen.findByText('修复 CSV')
  expect(list.container.querySelectorAll('tbody .wb-status')).toHaveLength(1)
})
it('uses hours for recent rows, dates for older records and absolute hover text', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-14T10:00:00Z'))
  expect(relativeTime('2026-09-14T07:00:00Z')).toBe('3 小时前')
  expect(relativeTime('2026-09-13T10:00:00Z')).toBe(new Date('2026-09-13T10:00:00Z').toLocaleDateString('zh-CN'))
  show(<ListTime value="2026-09-14T07:00:00Z" />)
  expect(screen.getByText('3 小时前').title).toContain('2026')
  expect(relativeTime('bad')).toBe('时间未记录')
})
it('all sidebar navigation links have actual non-empty accessible names and current-page semantics', () => {
  show(<Workbench {...props} user={props.user} onLogout={vi.fn()} />, '/ability-center')
  const nav = within(screen.getByRole('navigation',{name:'主导航'}))
  expect(nav.getAllByRole('link',{name:/.+/})).toHaveLength(nav.getAllByRole('link').length)
  expect(nav.getByRole('link',{name:'能力中心'}).getAttribute('aria-current')).toBe('page')
  expect(nav.queryByRole('link',{name:'能力模块'})).toBeNull(); expect(nav.queryByRole('link',{name:'经验库'})).toBeNull()
})
it('project cards have explicit accessible names', async () => {
  show(<ProjectsPage {...props} />)
  expect((await screen.findByRole('link',{name:'打开项目：工时项目'})).getAttribute('href')).toBe('/projects/p1')
})
it('persists manual theme selection, restores it and allows system following', () => {
  localStorage.setItem(THEME_KEY,'dark')
  const view = show(<ThemeSwitch />)
  expect(document.documentElement.dataset.theme).toBe('dark')
  fireEvent.change(screen.getByRole('combobox',{name:'外观主题'}),{target:{value:'light'}})
  expect(localStorage.getItem(THEME_KEY)).toBe('light'); expect(document.documentElement.dataset.theme).toBe('light')
  view.unmount(); show(<ThemeSwitch />)
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('light')
  fireEvent.change(screen.getByRole('combobox'),{target:{value:'system'}})
  expect(localStorage.getItem(THEME_KEY)).toBe('system'); expect(document.documentElement.dataset.theme).toBeUndefined()
})
it('ignores malformed theme and still switches when storage is unavailable', () => {
  localStorage.setItem(THEME_KEY,'bad'); show(<ThemeSwitch />)
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('system')
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(() => { throw new Error('blocked') })
  fireEvent.change(screen.getByRole('combobox'),{target:{value:'dark'}})
  expect(document.documentElement.dataset.theme).toBe('dark'); applyTheme('system')
})
const event = (id:number,type='model.output'): AuditEvent => ({ id, version:1, run_id:'run-one', type, at:'2026-09-14T01:00:00Z', payload:{message:`第 ${id} 条\n第二行`} })
it('follows log additions, pauses on up-scroll, and resumes only at latest', () => {
  const view = show(<ExecutionLog events={[event(1)]} />)
  const log = screen.getByRole('log')
  Object.defineProperties(log,{scrollHeight:{configurable:true,value:800},clientHeight:{configurable:true,value:200}})
  view.rerender(<MemoryRouter><ExecutionLog events={[event(1),event(2)]} /></MemoryRouter>)
  expect(log.scrollTop).toBe(800)
  log.scrollTop=120; fireEvent.scroll(log)
  expect(screen.getByRole('button',{name:'跳到最新'})).toBeTruthy()
  view.rerender(<MemoryRouter><ExecutionLog events={[event(1),event(2),event(3)]} /></MemoryRouter>)
  expect(log.scrollTop).toBe(120); expect(log.getAttribute('aria-live')).toBe('off')
  fireEvent.click(screen.getByRole('button',{name:'跳到最新'}))
  expect(log.scrollTop).toBe(800); expect(log.getAttribute('aria-live')).toBe('polite')
})
it('adds timestamps to every multiline log row and classifies output without HTML injection', () => {
  const view = show(<ExecutionLog events={[event(1,'command.started'),event(2),{...event(3,'attempt.failed'),payload:{error:'<script>alert(1)</script>'}}]} />)
  expect(view.container.querySelectorAll('.wb-log-line time')).toHaveLength(5)
  expect(view.container.querySelector('.wb-log-command')).toBeTruthy(); expect(view.container.querySelector('.wb-log-model')).toBeTruthy(); expect(view.container.querySelector('.wb-log-error')).toBeTruthy()
  expect(view.container.querySelector('script')).toBeNull()
})
function Location() { const location = useLocation(); return <output>{location.pathname+location.search+location.hash}</output> }
it.each(['modules','capabilities'] as const)('redirects old %s URLs while preserving selected item and fragment', tab => {
  show(<Routes><Route path={`/${tab}`} element={<LegacyCapabilityRedirect tab={tab} />} /><Route path="/ability-center" element={<Location />} /></Routes>,`/${tab}?selected=c1#detail`)
  expect(screen.getByText(`/ability-center?selected=c1&tab=${tab}#detail`)).toBeTruthy()
})
it('joins provenance through source run ids, deduplicates modules, and keeps selection in the capabilities tab', async () => {
  show(<><CapabilityCenter {...props} /><Location /></>, '/ability-center?tab=modules')
  await screen.findByText('由它沉淀的能力 1 项')
  expect(screen.getAllByRole('heading',{level:1})).toHaveLength(1)
  fireEvent.click(screen.getByRole('tab',{name:'沉淀能力'}))
  fireEvent.click(await screen.findByRole('button',{name:/工时维护/}))
  await screen.findByRole('heading',{name:'工时维护'})
  expect(screen.getByRole('tab',{name:'沉淀能力'}).getAttribute('aria-selected')).toBe('true')
  expect(screen.getAllByRole('link',{name:/来源模块：维护方法/})[0].getAttribute('href')).toBe('/ability-center?tab=modules&selected=m1')
  expect(screen.getAllByRole('link',{name:/来源运行 run-one/})[0].getAttribute('href')).toBe('/runs/run-one')
  await waitFor(() => expect(screen.getByText('/ability-center?selected=c1&tab=capabilities')).toBeTruthy())
})

it('attention hides replaced runs and includes spec confirmation; active includes analysis', async () => {
  const fixtures = [
    {...run,id:'old',request:'历史暂停',retry_run_id:'next'},
    {...run,id:'next',request:'规格确认',status:'awaiting_spec_confirmation'},
    {...run,id:'analysis',request:'需求分析中',status:'requirement_analysis'},
  ]
  const original = api.getMockImplementation()!
  api.mockImplementation(async (url,options) => url === '/api/v2/runs' ? {runs:fixtures} as never : original(url,options))
  show(<RunsPage {...props}/>, '/runs?filter=attention')
  await screen.findByText('规格确认')
  expect(screen.queryByText('历史暂停')).toBeNull()
  fireEvent.click(screen.getByRole('tab',{name:/进行中/}))
  await screen.findByRole('link',{name:'查看需求分析：需求分析中'})
})
it('legacy module cards expose distinguishing source summaries', async () => {
  const original = api.getMockImplementation()!
  api.mockImplementation(async (url,options) => url === '/api/v4/modules' ? {modules:[
    {...module,id:'legacy-one',name:'遗留能力',instructions:'维护旧系统。保留行为'},
    {...module,id:'legacy-two',name:'遗留能力',instructions:'生成命令行工具。验证输入'},
  ]} as never : original(url,options))
  show(<CapabilityCenter {...props}/>, '/ability-center')
  await screen.findByRole('heading',{name:/遗留能力 · 维护旧系统/})
  expect(screen.getByRole('heading',{name:/遗留能力 · 生成命令行工具/})).toBeTruthy()
})
