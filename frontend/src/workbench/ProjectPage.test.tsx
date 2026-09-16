// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ProjectPage from './ProjectPage'
import { request, WorkspaceApiError } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
vi.mock('./ProjectMode', () => ({ default: () => null }))
vi.mock('./ProjectKnowledge', () => ({ default: () => null }))
vi.mock('../workspace/ProjectAgent', () => ({ default: () => null }))
const api = vi.mocked(request)
const unauthorized = vi.fn()
const project = { id: 'p1', name: '示例项目', repository: 'local/sample', workspace: '/tmp/project', base_branch: 'main', checks: {}, revision: 1 }
const presets = [
  { id: 'release', version: 1, label: '部署准备', hint: '环境', fields: [] },
  { id: 'general', version: 1, label: '新需求', hint: '目标', fields: [] },
  { id: 'bugfix', version: 7, label: '数据库里的修复入口', hint: '描述问题', fields: [{ id: 'error_log', label: '错误日志' }, { id: 'reproduction', label: '复现步骤' }] },
]
function show() { return render(<MemoryRouter initialEntries={['/projects/p1']}><Routes><Route path="/projects/:projectId" element={<ProjectPage csrfToken="csrf" onUnauthorized={unauthorized} user={{ id: 1, username: 'owner', role: 'admin' }} />} /><Route path="/runs/:runId" element={<div>运行已接收</div>} /></Routes></MemoryRouter>) }
beforeEach(() => {
  localStorage.clear()
  api.mockReset()
  api.mockImplementation(async (url) => {
    if (url.includes('/deploy-targets')) return { targets: [], revision: 0, available: [] } as never
    if (url === '/api/v2/projects') return { projects: [project] } as never
    if (url === '/api/v2/operation-presets') return { presets } as never
    if (url.includes('/inspection')) return { enabled: false, interval_s: 3600, revision: 0 } as never
    if (url.includes('/readiness')) return { ready: true, checks: [] } as never
    if (url.includes('/overview')) return { project_id: 'p1', run_snapshots: [], runs: 0 } as never
    return {} as never
  })
})
afterEach(cleanup)
describe('ProjectPage maintenance entry', () => {
  it('uses catalog labels and optional fields from the API and submits structured input', async () => {
    show()
    fireEvent.click(await screen.findByText('高级操作'))
    fireEvent.click(await screen.findByRole('tab', { name: '数据库里的修复入口' }))
    fireEvent.change(screen.getByLabelText('数据库里的修复入口说明'), { target: { value: '保存失败' } })
    fireEvent.change(screen.getByLabelText('错误日志'), { target: { value: 'TypeError at save' } })
    fireEvent.change(screen.getByLabelText('复现步骤'), { target: { value: '点击保存' } })
    api.mockImplementationOnce(async () => ({ id: 'r1' }) as never)
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口' }))
    await screen.findByText('运行已接收')
    const call = api.mock.calls.find(([url, options]) => url === '/api/v2/runs' && options?.method === 'POST')!
    expect(call[1]?.body).toMatchObject({ project_id: 'p1', operation: 'bugfix', operation_fields: { error_log: 'TypeError at save', reproduction: '点击保存' } })
  })
  it('retains idempotency key on retry and leaves optional fields empty', async () => {
    show()
    fireEvent.click(await screen.findByText('高级操作'))
    fireEvent.click(await screen.findByRole('tab', { name: '数据库里的修复入口' }))
    fireEvent.change(screen.getByLabelText('数据库里的修复入口说明'), { target: { value: '保存失败' } })
    api.mockImplementationOnce(async () => { throw new Error('网络中断') })
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口' }))
    await screen.findByText('网络中断')
    api.mockImplementationOnce(async () => { throw new WorkspaceApiError(409, '内容冲突') })
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口' }))
    await screen.findByText('内容冲突')
    const bodies = api.mock.calls.filter(([url]) => url === '/api/v2/runs').map(([, options]) => options!.body as { idempotency_key: string; operation_fields: object })
    expect(bodies).toHaveLength(2)
    expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key)
    expect(bodies[0].operation_fields).toEqual({})
  })
  it('saves opt-in inspection with the current revision', async () => {
    show()
    fireEvent.click(await screen.findByRole('button', { name: '项目设置' }))
    const checkbox = await screen.findByRole('switch', { name: '启用巡检' })
    const intervals = screen.getByLabelText('巡检间隔')
    expect(Array.from(intervals.querySelectorAll('option')).map(option => [option.textContent, option.value])).toEqual([['5 分钟', '300'], ['15 分钟', '900'], ['1 小时', '3600'], ['6 小时', '21600'], ['1 天', '86400']])
    fireEvent.click(checkbox)
    await waitFor(() => expect(api.mock.calls.some(([url, options]) => url.endsWith('/inspection') && options?.method === 'PUT')).toBe(true))
    expect(api.mock.calls.find(([, options]) => options?.method === 'PUT')![1]?.body).toEqual({ enabled: true, interval_s: 3600, revision: 0, remote_read_only: false })
  })
})
it('requires an explicit release checkbox and includes it in the submission identity', async () => {
  show()
  fireEvent.click(await screen.findByText('高级操作'))
  fireEvent.click(await screen.findByRole('tab', { name: '部署准备' }))
  const checkbox = screen.getByRole('checkbox', { name: /执行部署：/ }) as HTMLInputElement
  expect(checkbox.checked).toBe(false)
  fireEvent.change(screen.getByLabelText('部署准备说明'), { target: { value: '准备部署' } })
  api.mockImplementationOnce(async () => { throw new Error('网络中断') })
  fireEvent.click(screen.getByRole('button', { name: '开始部署准备' }))
  await screen.findByText('网络中断')
  fireEvent.click(checkbox)
  api.mockImplementationOnce(async () => ({ id: 'r1' }) as never)
  fireEvent.click(screen.getByRole('button', { name: '开始部署准备' }))
  await screen.findByText('运行已接收')
  const bodies = api.mock.calls.filter(([url]) => url === '/api/v2/runs').map(([, options]) => options!.body as { execute_deploy: boolean; idempotency_key: string })
  expect(bodies.map(body => body.execute_deploy)).toEqual([false, true])
  expect(bodies[0].idempotency_key).not.toEqual(bodies[1].idempotency_key)
})

it('keeps settings out of the main flow and supports arrow keys between work-type tabs', async () => {
  show()
  fireEvent.click(await screen.findByText('高级操作'))
  const general = await screen.findByRole('tab', { name: '新需求' })
  expect(screen.queryByText('定时巡检')).toBeNull()
  expect(screen.queryByText('项目服务器')).toBeNull()
  general.focus(); fireEvent.keyDown(general, { key: 'ArrowRight' })
  const bugfix = screen.getByRole('tab', { name: '数据库里的修复入口' })
  expect(document.activeElement).toBe(bugfix)
  expect(bugfix.getAttribute('aria-selected')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: '项目设置' }))
  await screen.findByText('定时巡检'); await screen.findByText('项目服务器')
})

it('restores a per-project draft including fields and clears it only after successful keyboard submission', async () => {
  localStorage.setItem('webuddy:request:p1', JSON.stringify({ value:'保留导出格式', operation:'bugfix', fields:{error_log:'TypeError'} }))
  localStorage.setItem('webuddy:request:another',JSON.stringify({value:'另一个项目'}))
  show(); const input = await screen.findByLabelText('数据库里的修复入口说明') as HTMLTextAreaElement
  expect(input.value).toBe('保留导出格式'); expect((screen.getByLabelText('错误日志') as HTMLTextAreaElement).value).toBe('TypeError')
  api.mockImplementationOnce(async () => { throw new Error('网络中断') })
  fireEvent.keyDown(input,{key:'Enter',ctrlKey:true})
  await screen.findByText('网络中断'); expect(localStorage.getItem('webuddy:request:p1')).toContain('保留导出格式')
  api.mockImplementationOnce(async () => ({id:'r1'}) as never)
  fireEvent.keyDown(input,{key:'Enter',metaKey:true})
  await screen.findByText('运行已接收')
  expect(localStorage.getItem('webuddy:request:p1')).toBeNull(); expect(localStorage.getItem('webuddy:request:another')).toContain('另一个项目')
})
it('saves edits across unmount, shows remaining characters near the limit and does not submit composing text', async () => {
  const view = show(); const input = await screen.findByLabelText('新需求说明')
  fireEvent.change(input,{target:{value:'字'.repeat(49000)}})
  expect(screen.getByText('还可输入 1000 字')).toBeTruthy()
  fireEvent.keyDown(input,{key:'Enter',ctrlKey:true,isComposing:true})
  expect(api.mock.calls.some(([url])=>url==='/api/v2/runs')).toBe(false)
  view.unmount(); show()
  expect((await screen.findByLabelText('新需求说明') as HTMLTextAreaElement).value.length).toBe(49000)
})

it('shows budget controls and explicitly saves monitoring without a hidden dollar ceiling', async () => {
  show()
  fireEvent.click(await screen.findByRole('button', { name: '项目设置' }))
  const mode = await screen.findByLabelText('费用控制', { selector: 'select' })
  expect((mode as HTMLSelectElement).value).toBe('monitor')
  fireEvent.change(mode, { target: { value: 'enforce' } })
  expect((screen.getByLabelText('单次运行预算（美元）') as HTMLInputElement).value).toBe('100')
  fireEvent.change(mode, { target: { value: 'monitor' } })
  expect(screen.queryByLabelText('单次运行预算（美元）')).toBeNull()
  api.mockImplementationOnce(async () => ({ ...project, revision: 2, budget_usd: null }) as never)
  fireEvent.click(screen.getByRole('button', { name: '保存项目设置' }))
  await waitFor(() => expect(api.mock.calls.find(([url, options]) => url === '/api/v2/projects/p1' && options?.method === 'PUT')?.[1]?.body).toMatchObject({ budget_usd: null, revision: 1 }))
})

it('does not disguise an active-run conflict as a revision conflict', async () => {
  show(); fireEvent.click(await screen.findByRole('button', {name:'项目设置'}))
  await screen.findByLabelText('项目名称')
  api.mockImplementationOnce(async () => { throw new WorkspaceApiError(409, '项目存在进行中的运行，暂时不能修改设置') })
  fireEvent.click(screen.getByRole('button', {name:'保存项目设置'}))
  await screen.findByText('项目存在进行中的运行，暂时不能修改设置')
  expect(screen.queryByRole('button', {name:'我已审阅，保留我的修改并重试'})).toBeNull()
  expect((screen.getByRole('button', {name:'保存项目设置'}) as HTMLButtonElement).disabled).toBe(false)
})
it('defaults analysis to monitoring and clears a separate ceiling when monitoring is selected', async () => {
  show(); fireEvent.click(await screen.findByRole('button', {name:'项目设置'}))
  const analysis = await screen.findByLabelText('需求分析独立预算（美元）') as HTMLInputElement
  expect(analysis.value).toBe('')
  fireEvent.change(analysis,{target:{value:'5'}})
  expect(screen.getByText(/需求分析达到 \$5 会暂停/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('费用控制', {selector:'select'}),{target:{value:'monitor'}})
  expect(analysis.value).toBe('')
  expect(screen.getByText('需求分析仅监测，不设美元停止线。')).toBeTruthy()
  expect(screen.getByRole('checkbox',{name:'自动确认规格（默认关闭）'}).closest('label')?.className).toContain('wb-checkbox')
})

it('submits the ordinary goal automatically without choosing a work type', async () => {
  show()
  const input = await screen.findByLabelText('新需求说明')
  expect(screen.queryByRole('tab', { name: '数据库里的修复入口' })).toBeNull()
  fireEvent.change(input, { target: { value: '做一个订单搜索页面' } })
  api.mockImplementationOnce(async () => ({ id: 'r1' }) as never)
  fireEvent.click(screen.getByRole('button', { name: '开始制作' }))
  await screen.findByText('运行已接收')
  expect(api.mock.calls.find(([url]) => url === '/api/v2/runs')?.[1]?.body).toMatchObject({ operation: 'general', operation_fields: {}, interaction_mode: 'automatic' })
})
