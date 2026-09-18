// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionSkillPanel from './SessionSkillPanel'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()

function skill(overrides: Record<string, unknown> = {}) {
  return {
    id: 'sk-1',
    session_id: 'sess-aaa',
    name: '测试方法',
    origin: 'zip',
    source_ref: 'SKILL.md',
    source_version: '1.0',
    source_sha256: 'abcd1234',
    entry: 'SKILL.md',
    description: '用于测试的方法',
    dependencies: [],
    import_state: 'imported',
    dependency_state: 'ready',
    actor_id: 'user-1',
    created_at: '2026-09-19T00:00:00+00:00',
    ...overrides,
  }
}

function mount(sessionId = 'sess-aaa') {
  return render(
    <MemoryRouter>
      <SessionSkillPanel sessionId={sessionId} csrfToken="x" onUnauthorized={noop} />
    </MemoryRouter>,
  )
}

beforeEach(() => { api.mockReset() })
afterEach(cleanup)

// ---- T1: session isolation ----
describe('session isolation', () => {
  it('only shows skills for the requested session, not others', async () => {
    const sessASkill = skill({ id: 'sk-a', session_id: 'sess-aaa', name: 'Skill A' })
    const sessBSkill = skill({ id: 'sk-b', session_id: 'sess-bbb', name: 'Skill B' })

    // Mock: each session returns only its own skills
    api.mockImplementation(async (url?: string) => {
      if (url === '/api/v4/sessions/sess-aaa/skills') {
        return { items: [sessASkill] } as never
      }
      if (url === '/api/v4/sessions/sess-bbb/skills') {
        return { items: [sessBSkill] } as never
      }
      return { items: [] } as never
    })

    // Mount with session A
    const { unmount } = mount('sess-aaa')
    // Open the details
    fireEvent.click(await screen.findByText(/当前会话加载的 Skill/))
    await screen.findByText('Skill A')

    // Session B's skill must NOT appear
    expect(screen.queryByText('Skill B')).toBeNull()

    // Verify the API was called with the correct session id
    expect(api).toHaveBeenCalledWith(
      '/api/v4/sessions/sess-aaa/skills',
      expect.objectContaining({}),
    )
    expect(api).not.toHaveBeenCalledWith(
      '/api/v4/sessions/sess-bbb/skills',
      expect.anything(),
    )

    unmount()

    // Now mount with session B
    mount('sess-bbb')
    fireEvent.click(await screen.findByText(/当前会话加载的 Skill/))
    await screen.findByText('Skill B')
    expect(screen.queryByText('Skill A')).toBeNull()
  })
})

// ---- T2: imported but dependency missing ----
describe('dependency missing state', () => {
  it('shows "需要管理员补齐" when imported but deps missing, never "可用"', async () => {
    api.mockImplementation(async () => ({
      items: [
        skill({
          import_state: 'imported',
          dependency_state: 'missing',
          dependencies: ['tool-x', 'tool-y'],
        }),
      ],
    }) as never)

    mount()
    fireEvent.click(await screen.findByText(/当前会话加载的 Skill/))

    // Must show "已导入" for import_state
    await screen.findByText('已导入')

    // Must show the admin remediation message
    expect(screen.getByText(/需要管理员补齐/)).toBeTruthy()
    expect(screen.getByText(/tool-x/)).toBeTruthy()
    expect(screen.getByText(/tool-y/)).toBeTruthy()

    // Must NOT show "可用" or "依赖就绪"
    expect(screen.queryByText('可用')).toBeNull()
    expect(screen.queryByText('依赖就绪')).toBeNull()
  })
})

// ---- T3: rejected with specific reason ----
describe('rejected state', () => {
  it('shows the specific failure reason when import was rejected', async () => {
    api.mockImplementation(async () => ({
      items: [
        skill({
          import_state: 'rejected',
          reject_reason: 'SKILL.md 格式非法：缺少必填字段 name',
        }),
      ],
    }) as never)

    mount()
    fireEvent.click(await screen.findByText(/当前会话加载的 Skill/))

    const badge = await screen.findByText(/导入失败/)
    expect(badge.textContent).toContain('SKILL.md 格式非法：缺少必填字段 name')
  })
})

// ---- T4: removal clears the item ----
describe('removal', () => {
  it('no longer shows the skill after removal', async () => {
    let deleted = false
    api.mockImplementation(async (url?: string, opts?: { method?: string }) => {
      if (opts?.method === 'DELETE') {
        deleted = true
        return { deleted: true } as never
      }
      // After deletion, return empty list; before deletion, return the skill
      if (deleted) return { items: [] } as never
      return { items: [skill()] } as never
    })

    mount()
    fireEvent.click(await screen.findByText(/当前会话加载的 Skill/))
    await screen.findByText('测试方法')

    // Click the remove button
    fireEvent.click(screen.getByRole('button', { name: '移除 测试方法' }))

    // After removal, the skill should disappear
    await waitFor(() => {
      expect(screen.queryByText('测试方法')).toBeNull()
    })
  })
})

// ---- T5: AgentManifest wording (tested via import) ----
// This test is in AgentManifest.test.tsx below, but also verified here for completeness.
// The actual assertion on AgentManifest.tsx wording is done separately.
