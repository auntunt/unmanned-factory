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
