// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import AgentsPage from './AgentsPage'
import PackDetail from './PackDetail'
import { request } from '../workspace/api'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
vi.mock('./AgentManifest', () => ({ default: () => <div>规范</div> }))
vi.mock('./AgentEvolution', () => ({ default: () => null }))
const api = vi.mocked(request)
const a = { id: 'a1', name: '助手甲', purpose: '甲', active_version: 1, updated_at: '2026-09-20T00:00:00Z' }
const b = { ...a, id: 'a2', name: '助手乙' }
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), user: { id: 1, username: 'admin', role: 'admin' as const } }
const manifest = { name: '新工具', purpose: '测试', tool: { name: 'tool', entrypoint: 'tool/main.py', runtime: 'python3', timeout_seconds: 30 }, dependency_lock: { python: '3', packages: [] }, support_matrix: [], evaluation_policy: { required: true, test_set: 'fixtures/tests.json' } }
const version = { id: 'v1', pack_id: 'p1', version: 1, content_digest: 'a'.repeat(64), created_at: '2026-09-20T00:00:00Z', manifest, files: [], evaluation_id: 'e1', support_matrix: [], lifecycle: 'published' }
const detail = { id: 'p1', name: '新工具', purpose: '测试', owner_id: '1', can_maintain: true, versions: [version], draft: null, evaluations: [], bindings: [], environments: {} }
function setup() {
 api.mockReset()
 let bound = false
 api.mockImplementation(async (path, opts) => {
  if (path === '/api/v4/agents') return { agents: [a, b] } as never
  if (path === '/api/v4/agents/a1') return a as never
  if (path === '/api/v4/agents/a2') return b as never
  if (path === '/api/v4/capability-packs') return { packs: [{ ...detail, published_version: 1 }] } as never
  if (path === '/api/v4/capability-packs/p1') return detail as never
  if (path === '/api/v4/capability-packs/bindings' && opts?.method === 'POST') { bound = true; return { id: 'bind1' } as never }
  if (path.includes('/bindings/')) return { bindings: bound ? [{ id: 'bind1', pack_id: 'p1', pack_name: '新工具', version: 1, version_id: 'v1', revision: 1, environment: { status: 'unchecked' } }] : [] } as never
  if (path.includes('/preflight')) return { ready: true, message: '' } as never
  if (path.includes('/modules')) return { modules: [] } as never
  if (path.includes('/capabilities')) return { capabilities: [] } as never
  return { items: [], skills: [], assertions: [], proposals: [], conversations: [], versions: [] } as never
 })
}
function Switch() { const nav = useNavigate(); return <button onClick={() => nav('/agents/a2')}>切换乙</button> }
function show(path = '/agents/a1') {
 render(<MemoryRouter initialEntries={[path]}><Switch /><Routes><Route path='/agents/:agentId' element={<AgentsPage {...props} />} /><Route path='/ability-center/packs/:packId' element={<PackDetail {...props} />} /></Routes></MemoryRouter>)
}
afterEach(cleanup)
it('a delayed A response cannot replace B after navigation', async () => {
 setup()
 const normal = api.getMockImplementation()!
 let resolveA!: (value: unknown) => void
 let first = true
 api.mockImplementation((path, opts) => {
  if (path === '/api/v4/agents' && first) { first = false; return new Promise(resolve => { resolveA = resolve }) as never }
  return normal(path, opts)
 })
 show()
 await waitFor(() => expect(resolveA).toBeDefined())
 fireEvent.click(screen.getByRole('button', { name: '切换乙' }))
 await screen.findByRole('heading', { name: '助手乙' })
 await act(async () => { resolveA({ agents: [a, b] }); await Promise.resolve() })
 expect(screen.queryByRole('heading', { name: '助手甲' })).toBeNull()
 expect(screen.getByRole('heading', { name: '助手乙' })).toBeTruthy()
})
it('successful attach immediately updates the attached tools section', async () => {
 setup(); show()
 await screen.findByRole('heading', { name: '助手甲' })
 fireEvent.click(screen.getByRole('button', { name: '团队已有能力' }))
 fireEvent.click(await screen.findByRole('button', { name: '挂靠到本职能体' }))
 await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/capability-packs/bindings', expect.objectContaining({ method: 'POST', body: { agent_id: 'a1', version_id: 'v1', expected_revision: 0 } })))
 await within(screen.getByRole('region', { name: '已挂靠工具' })).findByText('新工具')
})
it('pack detail uses its agent context without requiring another role choice', async () => {
 setup(); show('/ability-center/packs/p1?agent_id=a2')
 await screen.findByRole('heading', { name: '新工具' })
 await screen.findByRole('option', { name: '助手乙' })
 expect((screen.getByLabelText(/目标职能体/) as HTMLSelectElement).value).toBe('a2')
 fireEvent.click(screen.getByRole('button', { name: '挂靠 v1' }))
 await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v4/capability-packs/bindings', expect.objectContaining({ body: { agent_id: 'a2', version_id: 'v1', expected_revision: 0 } })))
})
it('pack detail provides a way back to the originating agent', async () => {
 setup(); show('/ability-center/packs/p1?agent_id=a2')
 await screen.findByRole('heading', { name: '新工具' })
 expect(screen.getAllByRole('link').some(link => link.getAttribute('href') === '/agents/a2')).toBe(true)
})
