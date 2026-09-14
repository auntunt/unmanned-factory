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
  { id: 'general', version: 1, label: '新需求', hint: '目标', fields: [] },
  { id: 'bugfix', version: 7, label: '数据库里的修复入口', hint: '描述问题', fields: [{ id: 'error_log', label: '错误日志' }, { id: 'reproduction', label: '复现步骤' }] },
]
function show() { return render(<MemoryRouter initialEntries={['/projects/p1']}><Routes><Route path="/projects/:projectId" element={<ProjectPage csrfToken="csrf" onUnauthorized={unauthorized} user={{ id: 1, username: 'owner', role: 'admin' }} />} /><Route path="/runs/:runId" element={<div>运行已接收</div>} /></Routes></MemoryRouter>) }
beforeEach(() => {
  api.mockReset()
  api.mockImplementation(async (url) => {
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
    fireEvent.click(await screen.findByRole('button', { name: '数据库里的修复入口' }))
    fireEvent.change(screen.getByLabelText('数据库里的修复入口说明'), { target: { value: '保存失败' } })
    fireEvent.change(screen.getByLabelText('错误日志'), { target: { value: 'TypeError at save' } })
    fireEvent.change(screen.getByLabelText('复现步骤'), { target: { value: '点击保存' } })
    api.mockImplementationOnce(async () => ({ id: 'r1' }) as never)
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口 →' }))
    await screen.findByText('运行已接收')
    const call = api.mock.calls.find(([url, options]) => url === '/api/v2/runs' && options?.method === 'POST')!
    expect(call[1]?.body).toMatchObject({ project_id: 'p1', operation: 'bugfix', operation_fields: { error_log: 'TypeError at save', reproduction: '点击保存' } })
  })
  it('retains idempotency key on retry and leaves optional fields empty', async () => {
    show()
    fireEvent.click(await screen.findByRole('button', { name: '数据库里的修复入口' }))
    fireEvent.change(screen.getByLabelText('数据库里的修复入口说明'), { target: { value: '保存失败' } })
    api.mockImplementationOnce(async () => { throw new Error('网络中断') })
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口 →' }))
    await screen.findByText('网络中断')
    api.mockImplementationOnce(async () => { throw new WorkspaceApiError(409, '内容冲突') })
    fireEvent.click(screen.getByRole('button', { name: '开始数据库里的修复入口 →' }))
    await screen.findByText('内容冲突')
    const bodies = api.mock.calls.filter(([url]) => url === '/api/v2/runs').map(([, options]) => options!.body as { idempotency_key: string; operation_fields: object })
    expect(bodies).toHaveLength(2)
    expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key)
    expect(bodies[0].operation_fields).toEqual({})
  })
  it('saves opt-in inspection with the current revision', async () => {
    show()
    const checkbox = await screen.findByRole('checkbox', { name: '启用巡检' })
    const intervals = screen.getByLabelText('巡检间隔')
    expect(Array.from(intervals.querySelectorAll('option')).map(option => [option.textContent, option.value])).toEqual([['5 分钟', '300'], ['15 分钟', '900'], ['1 小时', '3600'], ['6 小时', '21600'], ['1 天', '86400']])
    fireEvent.click(checkbox)
    await waitFor(() => expect(api.mock.calls.some(([url, options]) => url.endsWith('/inspection') && options?.method === 'PUT')).toBe(true))
    expect(api.mock.calls.find(([, options]) => options?.method === 'PUT')![1]?.body).toEqual({ enabled: true, interval_s: 3600, revision: 0 })
  })
})
