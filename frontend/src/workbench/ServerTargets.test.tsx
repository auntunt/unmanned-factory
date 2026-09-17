// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ServerTargets, { ProjectTargets } from './ServerTargets'
import OperationResults from './OperationResults'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api', () => ({ request: vi.fn() }))
const api = vi.mocked(request)
const onUnauthorized = vi.fn()
const target = { id: 't1', name: '生产服务', host: 'server.test', port: 22, user: 'deploy', revision: 3, public_key: 'ssh-ed25519 public', public_fingerprint: 'SHA256:public', host_fingerprint: 'SHA256:host', commands: {} }

beforeEach(() => { api.mockReset() })
afterEach(cleanup)

function mockTargetsAndChecks(targets: unknown[], checks: Record<string, unknown> = {}) {
  api.mockImplementation(((path: string) => {
    if (path === '/api/v2/deploy-targets') return Promise.resolve({ targets })
    if (path === '/api/v2/deploy-targets/checks') return Promise.resolve({ checks })
    return Promise.resolve({})
  }) as typeof request)
}

it('shows empty state when no targets are configured', async () => {
  mockTargetsAndChecks([])
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('尚未配置公司测试环境')
  expect(screen.getByText(/添加至少一个部署目标/)).toBeTruthy()
  expect(screen.getByText('注册第一个目标')).toBeTruthy()
})

it('shows loading state initially', () => {
  api.mockImplementation(() => new Promise(() => {}))
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  expect(screen.getByText('正在读取服务器配置…')).toBeTruthy()
})

it('shows target with last check result and time', async () => {
  mockTargetsAndChecks([target], { t1: { status: 'pass', reason: '', checked_at: '2026-09-17T10:00:00Z' } })
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
  expect(screen.getByText('通过')).toBeTruthy()
  expect(screen.getByText('deploy@server.test:22')).toBeTruthy()
  expect(screen.getByText(/SHA256:public/)).toBeTruthy()
})

it('shows "未检查" when target has no check history', async () => {
  mockTargetsAndChecks([target], {})
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
  expect(screen.getByText('未检查')).toBeTruthy()
})

it('shows check failure reason', async () => {
  mockTargetsAndChecks([target], { t1: { status: 'unverified', reason: '无法取得匹配的主机公钥指纹，已拒绝连接', checked_at: '2026-09-17T09:00:00Z' } })
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
  expect(screen.getByText('未验证')).toBeTruthy()
  expect(screen.getByText('无法取得匹配的主机公钥指纹，已拒绝连接')).toBeTruthy()
})

it('triggers connection check and updates result', async () => {
  mockTargetsAndChecks([target], {})
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
  api.mockResolvedValueOnce({ status: 'pass', reason: '' } as never)
  fireEvent.click(screen.getByText('检查连接'))
  await screen.findByRole('status')
  // The test endpoint is called correctly
  const testCall = api.mock.calls.find(c => c[0] === '/api/v2/deploy-targets/t1/test')
  expect(testCall).toBeTruthy()
  expect(testCall![1]?.body).toBeUndefined()
  // Check result now shows pass
  expect(screen.getByText('通过')).toBeTruthy()
})

it('recovers from load error on retry', async () => {
  api.mockRejectedValue(new Error('network'))
  const { unmount } = render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('network')
  unmount()
  // Re-render with working API
  mockTargetsAndChecks([target], {})
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
})

it('has the company-environment anchor id', async () => {
  mockTargetsAndChecks([target], {})
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('生产服务')
  const section = document.getElementById('company-environment')
  expect(section).toBeTruthy()
  expect(section?.tagName).toBe('SECTION')
})

it('shows the public key and issues only the fixed connection test endpoint', async () => {
  mockTargetsAndChecks([target])
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('ssh-ed25519 public')
  api.mockResolvedValueOnce({ status: 'pass', reason: '' } as never)
  fireEvent.click(screen.getByText('检查连接'))
  await screen.findByRole('status')
  const testCall = api.mock.calls.find(c => c[0] === '/api/v2/deploy-targets/t1/test')
  expect(testCall).toBeTruthy()
  expect(testCall![1]?.body).toBeUndefined()
})

it('member project bindings are read only and never load the administrator catalog', async () => {
  api.mockResolvedValue({ targets: ['t1'], revision: 1, available: [target] } as never)
  render(<ProjectTargets projectId="p1" csrfToken="csrf" onUnauthorized={onUnauthorized} isAdmin={false} />)
  await screen.findByText(/生产服务/ )
  expect(screen.queryByRole('checkbox')).toBeNull()
  expect(screen.getByText('未验证')).toBeTruthy()
  expect(screen.queryByText('保存项目绑定')).toBeNull()
  expect(api.mock.calls).toHaveLength(1)
})

it('release results distinguish no connection and unverified remote delivery', () => {
  const { rerender } = render(<OperationResults run={{ source: { operation: 'release' }, artifacts: {} } as unknown as Run} />)
  expect(screen.getByText('未连接服务器，仅完成准备')).toBeTruthy()
  rerender(<OperationResults run={{ source: { operation: 'release', remote_targets: [target] }, artifacts: { remote_results: [{ target_id: 't1', verb: 'deploy', status: 'unverified', exit_code: 1, executed: true, reason: '脚本失败' }] } } as unknown as Run} />)
  expect(screen.getByText('生产服务')).toBeTruthy()
  expect(screen.getByText(/部署：未验证 · 已执行 · 退出码 1/)).toBeTruthy()
})

it('uses the shared form layout for server registration controls', async () => {
  mockTargetsAndChecks([target])
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  const name = await screen.findByLabelText('目标名称')
  expect(name.closest('form')?.className).toContain('wb-form')
  expect(name.closest('.wb-form-grid')).toBeTruthy()
})
