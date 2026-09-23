// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import MonitorPage from './MonitorPage'
import type { Overview } from './types'

const noop = vi.fn()

function baseOverview(overrides: Partial<Overview> = {}): Overview {
  return {
    contract_version: 'maintenance-subsystem/1',
    generated_at: '2026-09-23T02:00:00Z',
    window: { kind: 'day', start: '2026-09-23T00:00:00Z', end: '2026-09-23T23:59:59Z', timezone: 'Asia/Shanghai' },
    scope: { project_ids: ['proj-1'] },
    sources: {
      maintenance: { status: 'connected' },
      executor: { status: 'online' },
      server_metrics: { status: 'not_connected', detail: '未配置采集源' },
      app_probes: { status: 'not_connected', detail: '未配置探针' },
      alerts: { status: 'not_connected' },
    },
    counts: { projects: 1, running: 1, attention: { total: 1, answer: 1, approval: 0, blocked: 0 }, delivered_in_window: 2 },
    attention: [{ task_id: 'task-1', project_id: 'proj-1', project_name: '企业报表系统', title: '金额汇总差异', kind: 'answer', reason: '需要确认口径', since: null }],
    projects: [{ project_id: 'proj-1', name: '企业报表系统', repository: 'org/repo', repo_state: 'ready', repo_state_label: '可开始维护', running: 1, waiting: 0, delivered: 2, delivery_target: '补丁', service_status: 'not_connected' }],
    events: [{ task_id: 'task-1', project_id: 'proj-1', execution_id: 'run-1', sequence: 1, kind: 'question', label: '等待业务回答', at: '2026-09-23T01:55:00Z' }],
    queue: { pending: 0, running: 1, executor: { status: 'online' } },
    availability: { state: 'enabled' },
    partial_errors: [],
    ...overrides,
  }
}

function mockFetchSequence(responses: (Overview | { status: number; detail: string })[]) {
  let call = 0
  globalThis.fetch = vi.fn(async () => {
    const item = responses[Math.min(call, responses.length - 1)]
    call += 1
    if (typeof item === 'object' && item !== null && 'status' in item && typeof (item as { status: unknown }).status === 'number' && 'detail' in item) {
      const err = item as { status: number; detail: string }
      return new Response(JSON.stringify({ detail: err.detail }), { status: err.status })
    }
    return new Response(JSON.stringify(item), { status: 200 })
  }) as unknown as typeof fetch
}

function renderPage(projectId?: string) {
  return render(
    <MemoryRouter>
      <MonitorPage csrfToken="csrf" onUnauthorized={noop} projectId={projectId} />
    </MemoryRouter>,
  )
}

beforeEach(() => { noop.mockReset() })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('MonitorPage 运维监控', () => {
  it('计数为 null 时显示 —，不伪造 0', async () => {
    mockFetchSequence([baseOverview({ counts: { projects: 1, running: null, attention: null, delivered_in_window: null } })])
    renderPage()
    await waitFor(() => expect(screen.getByText('维护项目')).toBeTruthy())
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(2)
  })

  it('未接入的数据源渲染为未接入，不假装已连接', async () => {
    mockFetchSequence([baseOverview()])
    renderPage()
    await waitFor(() => expect(screen.getByText(/服务器指标/)).toBeTruthy())
    expect(screen.getByText(/服务器指标 · 未接入/)).toBeTruthy()
    expect(screen.getByText(/应用告警\/探针 · 未接入/)).toBeTruthy()
    expect(screen.getAllByText('未接入').length).toBeGreaterThanOrEqual(2) // CPU/内存、应用可用性
  })

  it('第二次轮询失败后保留旧数据并显示过期横幅', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetchSequence([baseOverview(), { status: 500, detail: '服务不可用' }])
    renderPage()
    await vi.waitFor(() => expect(screen.getByText('维护项目')).toBeTruthy())
    expect(screen.queryByRole('status')).toBeNull()

    await vi.advanceTimersByTimeAsync(15000)
    await vi.waitFor(() => expect(screen.getByRole('status')).toBeTruthy())
    expect(screen.getByText(/连接中断/)).toBeTruthy()
    // 旧数据仍然可见
    expect(screen.getByText('企业报表系统 · 金额汇总差异')).toBeTruthy()
  })

  it('首次加载失败显示错误与重试', async () => {
    mockFetchSequence([{ status: 500, detail: '服务器内部错误' }])
    renderPage()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/服务器内部错误/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
  })

  it('403 显示无权限状态', async () => {
    mockFetchSequence([{ status: 403, detail: '无权限' }])
    renderPage()
    await waitFor(() => expect(screen.getByText('无权限')).toBeTruthy())
  })

  it('待处理列表的链接指向任务现场', async () => {
    mockFetchSequence([baseOverview()])
    renderPage()
    await waitFor(() => expect(screen.getByText('进入现场')).toBeTruthy())
    const link = screen.getByText('进入现场').closest('a')
    expect(link?.getAttribute('href')).toBe('/maintenance/task-1')
  })

  it('没有项目时显示接入引导', async () => {
    mockFetchSequence([baseOverview({ counts: { projects: 0, running: 0, attention: { total: 0, answer: 0, approval: 0, blocked: 0 }, delivered_in_window: 0 }, scope: { project_ids: [] }, projects: [], attention: [], events: [] })])
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有接入代码库')).toBeTruthy())
    const link = screen.getByText('去接入代码库')
    expect(link.closest('a')?.getAttribute('href')).toBe('/maintenance/repos')
  })
})

describe('嵌入挂载', () => {
  it('在 /embed/maintenance 下，进入现场与接入链接都留在嵌入前缀里，不跳进主壳', async () => {
    const { default: EmbeddedMaintenance } = await import('./EmbeddedMaintenance')
    const { Route, Routes } = await import('react-router-dom')
    mockFetchSequence([baseOverview()])
    render(
      <MemoryRouter initialEntries={['/embed/maintenance']}>
        <Routes>
          <Route path="embed/maintenance/*" element={<EmbeddedMaintenance basePath="/embed/maintenance" csrfToken="csrf" onUnauthorized={noop} />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('进入现场')).toBeTruthy())
    expect(screen.getByText('进入现场').getAttribute('href')).toBe('/embed/maintenance/task-1')
    for (const link of Array.from(document.querySelectorAll('a[href^="/"]'))) {
      expect(link.getAttribute('href')!.startsWith('/embed/maintenance')).toBe(true)
    }
  })
})

describe('MonitorPage 项目行下钻', () => {
  const row = (id: string, state: 'needs_input' | 'failed' | 'ready', name: string) => ({
    project_id: id, name, repository: 'org/r', repo_state: state, repo_state_label: state,
    running: 0, waiting: 0, delivered: 0, delivery_target: '补丁', service_status: 'not_connected' })

  it('项目名与下一步链接到仓库详情，状态徽标不是链接；嵌入前缀下仍留在嵌入路由', async () => {
    const { MaintenanceBaseContext } = await import('./base-path')
    for (const base of ['/maintenance', '/embed/maintenance']) {
      mockFetchSequence([baseOverview({ projects: [row('p/1', 'needs_input', '待补充仓库'), row('p2', 'failed', '失败仓库'), row('p3', 'ready', '就绪仓库')] })])
      render(<MaintenanceBaseContext.Provider value={base}><MemoryRouter><MonitorPage csrfToken="c" onUnauthorized={noop} /></MemoryRouter></MaintenanceBaseContext.Provider>)
      const name = await screen.findByRole('link', { name: '待补充仓库' })
      expect(name.getAttribute('href')).toBe(`${base}/repos/p%2F1`)
      expect(screen.getByRole('link', { name: '查看待补充项' }).getAttribute('href')).toBe(`${base}/repos/p%2F1`)
      expect(screen.getByRole('link', { name: '查看接入失败原因' }).getAttribute('href')).toBe(`${base}/repos/p2`)
      expect(screen.getByRole('link', { name: '查看项目' }).getAttribute('href')).toBe(`${base}/repos/p3`)
      expect(screen.getByText('needs_input').closest('a')).toBeNull()
      expect(screen.getByRole('link', { name: '维护代码库' }).getAttribute('href')).toBe(`${base}/repos`)
      cleanup()
    }
  })
})
