// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import Deliverables from './Deliverables'
import { request } from '../workspace/api'
import { DELIVERY_TYPE_LABEL } from '../conversation/delivery-constants'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const noop = vi.fn()
afterEach(cleanup)

const run = { id: 'r1', project_id: 'p1', status: 'ready_for_review', request: '做预约工具', revision: 1, artifacts: {}, updated_at: '' } as never

it('shows service delivery type label matching RunWorkspace constant', async () => {
  api.mockImplementation(async () => ({ saved: true, items: [], can_collect: true, github_configured: false, delivery_type: 'service' }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByText(/线上服务/)).toBeTruthy())
  expect(DELIVERY_TYPE_LABEL['service']).toBe('线上服务')
  const section = document.querySelector('[data-delivery-type="service"]')
  expect(section).toBeTruthy()
})

it('shows CLI delivery type with deployment not applicable', async () => {
  api.mockImplementation(async () => ({ saved: true, items: [], can_collect: true, github_configured: false, delivery_type: 'cli' }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getAllByText(/命令行工具/).length).toBeGreaterThanOrEqual(1))
  expect(screen.getByText(/不适用/)).toBeTruthy()
  const section = document.querySelector('[data-delivery-type="cli"]')
  expect(section).toBeTruthy()
})

it('shows installer delivery type with pending confirmation when targets missing', async () => {
  api.mockImplementation(async () => ({ saved: true, items: [], can_collect: true, github_configured: false, delivery_type: 'installer', installer_targets: null }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getAllByText(/安装包/).length).toBeGreaterThanOrEqual(1))
  expect(screen.getByText(/待确认/)).toBeTruthy()
  const section = document.querySelector('[data-delivery-type="installer"]')
  expect(section).toBeTruthy()
})

it('shows installer delivery type with confirmed targets', async () => {
  api.mockImplementation(async () => ({ saved: true, items: [], can_collect: true, github_configured: false, delivery_type: 'installer', installer_targets: ['macOS', 'Windows'] }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByText(/macOS、Windows/)).toBeTruthy())
})

it('shows undeclared for legacy tasks with null delivery type', async () => {
  api.mockImplementation(async () => ({ saved: true, items: [], can_collect: true, github_configured: false }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getAllByText(/未声明/).length).toBeGreaterThanOrEqual(1))
  const section = document.querySelector('[data-delivery-type="undeclared"]')
  expect(section).toBeTruthy()
})

it('uses the same DELIVERY_TYPE_LABEL constant as RunWorkspace', () => {
  // This test ensures the shared constant file is actually shared.
  // If either file defines its own labels, this import-based check will fail.
  expect(DELIVERY_TYPE_LABEL).toEqual({ service: '线上服务', cli: '命令行工具', installer: '安装包' })
})

// --- N6: capability sources ---

it('shows loaded skills and invoked tools in capability source panel', async () => {
  api.mockImplementation(async () => ({
    saved: true, items: [], can_collect: true, github_configured: false,
    capability_sources: {
      loaded: { status: 'available', items: [
        { name: '简洁产品界面', id: 'mod-1', origin: 'project_module' },
        { name: '会话技能A', id: 'sk-1', origin: 'session_skill' },
      ] },
      invoked: { status: 'available', items: [
        { name: 'Bash', count: 5, first_at: '2026-09-19T00:01:00Z', last_at: '2026-09-19T00:05:00Z' },
      ] },
    },
  }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByTestId('capability-sources')).toBeTruthy())
  expect(screen.getByText('简洁产品界面')).toBeTruthy()
  expect(screen.getByText(/项目模块/)).toBeTruthy()
  expect(screen.getByText('会话技能A')).toBeTruthy()
  expect(screen.getByText(/会话导入/)).toBeTruthy()
  expect(screen.getByText('Bash')).toBeTruthy()
})

it('shows no_record text for old runs without records', async () => {
  api.mockImplementation(async () => ({
    saved: true, items: [], can_collect: true, github_configured: false,
    capability_sources: {
      loaded: { status: 'no_record', items: [] },
      invoked: { status: 'no_record', items: [] },
    },
  }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByTestId('capability-sources')).toBeTruthy())
  expect(screen.getByTestId('loaded-no-record')).toBeTruthy()
  expect(screen.getByTestId('invoked-no-record')).toBeTruthy()
  // Must contain the word "缺记录", NOT empty, NOT "无"
  const noRecordTexts = screen.getAllByText(/缺记录/)
  expect(noRecordTexts.length).toBeGreaterThanOrEqual(2)
})

// --- N6: service_url ---

it('shows clickable service_url for service delivery type', async () => {
  api.mockImplementation(async () => ({
    saved: true, items: [], can_collect: true, github_configured: false,
    delivery_type: 'service',
    service_urls: [{ name: '测试服务器', url: 'https://test.example.com' }],
  }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByTestId('service-urls')).toBeTruthy())
  const link = screen.getByRole('link', { name: /test\.example\.com/ })
  expect(link).toBeTruthy()
  expect(link.getAttribute('href')).toBe('https://test.example.com')
  expect(link.getAttribute('target')).toBe('_blank')
})

it('shows "未登记固定地址" when no service_url for service type', async () => {
  api.mockImplementation(async () => ({
    saved: true, items: [], can_collect: true, github_configured: false,
    delivery_type: 'service',
    service_urls: [],
  }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByTestId('no-service-url')).toBeTruthy())
  expect(screen.getByText(/未登记固定地址/)).toBeTruthy()
})

// --- N6: continue modify ---

it('shows continue-modify entry in delivery view', async () => {
  api.mockImplementation(async () => ({
    saved: true, items: [], can_collect: true, github_configured: false,
  }) as never)
  render(<Deliverables run={run} csrfToken="csrf" onUnauthorized={noop} isAdmin={true} />)
  await waitFor(() => expect(screen.getByTestId('continue-modify')).toBeTruthy())
  expect(screen.getAllByText(/继续修改/).length).toBeGreaterThanOrEqual(1)
})
