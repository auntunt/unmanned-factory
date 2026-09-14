// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ServerTargets, { ProjectTargets } from './ServerTargets'
import OperationResults from './OperationResults'
import { request } from '../workspace/api'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api', () => ({ request: vi.fn() }))
const api = vi.mocked(request)
const onUnauthorized = vi.fn()
const target = { id: 't1', name: '生产服务', host: 'server.test', port: 22, user: 'deploy', revision: 3, public_key: 'ssh-ed25519 public', public_fingerprint: 'SHA256:public', host_fingerprint: 'SHA256:host', commands: {} }
beforeEach(() => { api.mockReset(); api.mockResolvedValue({ targets: [target] } as never) })
afterEach(cleanup)
it('shows the public key and issues only the fixed connection test endpoint', async () => {
  render(<ServerTargets csrfToken="csrf" onUnauthorized={onUnauthorized} />)
  await screen.findByText('ssh-ed25519 public')
  api.mockResolvedValueOnce({ status: 'pass' } as never)
  fireEvent.click(screen.getByText('测试连接'))
  await screen.findByRole('status')
  expect(api.mock.calls[1][0]).toBe('/api/v2/deploy-targets/t1/test')
  expect(api.mock.calls[1][1]?.body).toBeUndefined()
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
