import { describe, expect, it } from 'vitest'
import { normalizeConfiguration, probeFor, validateDraft } from './RuntimePage'
import type { RuntimeConfiguration, RuntimeProbe } from './runtime-types'

const configured: RuntimeConfiguration = {
  revision: 4,
  profiles: {
    planner: { provider: 'codex', model: 'planner-model' },
    cheap: { provider: 'claude', model: 'cheap-model' },
    standard: { provider: 'codex', model: 'standard-model' },
    strong: { provider: 'claude', model: 'strong-model' },
  },
  limits: { timeout_s: 600, max_parallel: 2, max_tasks: 20, unknown_cost_policy: 'stop' },
}

describe('runtime workbench helpers', () => {
  it('normalizes diagnostic configuration revisions and keeps all four roles', () => {
    const result = normalizeConfiguration({ configuration_revision: 7, profiles: configured.profiles, limits: configured.limits, tools: [], blockers: [], last_probes: [] })
    expect(result.revision).toBe(7)
    expect(Object.keys(result.profiles)).toEqual(['planner', 'cheap', 'standard', 'strong'])
  })

  it('blocks unsupported planner provider and accepts bounded cost policy', () => {
    expect(validateDraft({ ...configured, profiles: { ...configured.profiles, planner: { provider: 'dsh', model: 'model' } } })).toContain('DSH')
    expect(validateDraft({ ...configured, limits: { ...configured.limits, unknown_cost_policy: 'allow_bounded' } })).toBeNull()
  })

  it('only exposes probes from the current configuration revision', () => {
    const probes: RuntimeProbe[] = [
      { id: 'old', profile: 'planner', provider: 'codex', model: 'm', configuration_revision: 3, checked_at: '2026-01-01', outcome: 'passed', message: 'old' },
      { id: 'new', profile: 'planner', provider: 'codex', model: 'm', configuration_revision: 4, checked_at: '2026-01-02', outcome: 'failed', message: 'new' },
    ]
    expect(probeFor(probes, 'planner', 4)?.id).toBe('new')
  })
})
