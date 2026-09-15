// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { request } from '../workspace/api'
import SkillIngestion, { IngestionReview } from './SkillIngestion'
import SkillTargetAuthorization from './SkillTargetAuthorization'
import type { Run } from '../workspace/types'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const api = vi.mocked(request)
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }
const draft = { id: 'draft', run_id: 'run', revision: 4, status: 'review', source_sha256: 'hash', mapping: {
  identity: '身份建议', steps: [{ title: '根路由', skill_path: 'SKILL.md', role: 'router', assertions: [{ text: 'MUST gate', kind: 'advisory', check: '人核对', basis: 'MUST gate' }] }],
  decisions: [{ path: 'run.sh', primitive: 'run.sh', target: 'unsupported', basis: '不可执行' }],
  dependencies: [{ path: 'SKILL.md', reason: 'tool dependency' }], injection_risks: [{ path: 'SKILL.md', reason: 'ignore previous instructions' }],
  skills: [{ path: 'SKILL.md', name: '攻击检查', requires_authorization: true, sha256: 'skillhash', body: '<script>bad()</script>' }],
} }
beforeEach(() => { api.mockReset(); api.mockResolvedValue({}) })
afterEach(cleanup)
it('shows mapping, assertions, unsupported, provenance and inert risks; requires human confirmation', async () => {
  render(<MemoryRouter><IngestionReview draft={draft} onChanged={vi.fn()} {...props} /></MemoryRouter>)
  expect(screen.getByText('路由根：根路由')).toBeTruthy()
  expect(screen.getByText(/该能力在本平台不可用/)).toBeTruthy()
  expect(screen.getByText(/ignore previous/)).toBeTruthy()
  expect(document.querySelector('script')).toBeNull()
  const sign = screen.getByRole('button', { name: '人签并启用职能包' }) as HTMLButtonElement
  expect(sign.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('人签身份段'), { target: { value: '人工身份' } })
  fireEvent.click(screen.getByLabelText(/我已核对身份/))
  fireEvent.click(sign)
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/skill-ingestions/draft/sign', expect.objectContaining({ body: { revision: 4, identity: '人工身份', authorization: { 'SKILL.md': true } } })))
})
it('never exposes signing for an unverified package', () => {
  render(<MemoryRouter><IngestionReview draft={{ ...draft, status: 'verifying' }} onChanged={vi.fn()} {...props} /></MemoryRouter>)
  expect(screen.queryByRole('button', { name: '人签并启用职能包' })).toBeNull()
})
it('submits only a selected project and mounted relative path', async () => {
  api.mockImplementation(async path => path === '/api/v2/projects' ? { projects: [{ id: 'p1', name: '测试项目' }] } : { items: [] })
  render(<MemoryRouter><SkillIngestion {...props} /></MemoryRouter>)
  fireEvent.click(screen.getByText('导入 skill 包 · 适配与人签'))
  await screen.findByRole('option', { name: '测试项目' })
  fireEvent.change(screen.getByLabelText('所属项目'), { target: { value: 'p1' } })
  fireEvent.change(screen.getByLabelText(/已挂载目录/), { target: { value: 'skills/demo' } })
  fireEvent.click(screen.getByRole('button', { name: '读取目录并开始适配' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/skill-ingestions/directory', expect.objectContaining({ body: { project_id: 'p1', path: 'skills/demo' } })))
})
it('requires explicit confirmation of targets and posts the current run revision', async () => {
  const run = { id: 'paused', revision: 2, status: 'needs_human', error: 'requires_authorization' } as unknown as Run
  render(<SkillTargetAuthorization run={run} {...props} />)
  const button = screen.getByRole('button', { name: '确认目标授权' }) as HTMLButtonElement
  expect(button.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText(/授权目标（/), { target: { value: 'test.example\n127.0.0.1' } })
  fireEvent.click(screen.getByLabelText(/我有权授权/))
  fireEvent.click(button)
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/runs/paused/skill-target-authorization', expect.objectContaining({ body: { revision: 2, targets: ['test.example', '127.0.0.1'] } })))
})
