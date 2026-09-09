import { describe, expect, it } from 'vitest'
import { createPollingGate, isUnauthorized } from './usePolling'

describe('usePolling auth failures', () => {
  it('recognizes the API error shape used for expired sessions', () => {
    expect(isUnauthorized({ status: 401 })).toBe(true)
    expect(isUnauthorized({ response: { status: 401 } })).toBe(true)
    expect(isUnauthorized({ status: 403 })).toBe(false)
    expect(isUnauthorized(new Error('登录已过期'))).toBe(false)
  })

  it('stops subsequent timer ticks after an expired session', async () => {
    let calls = 0
    const gate = createPollingGate(async () => {
      calls += 1
      if (calls === 1) throw { response: { status: 401 } }
      return 'ok'
    })
    await expect(gate.run()).rejects.toMatchObject({ response: { status: 401 } })
    await expect(gate.run()).resolves.toEqual({ stopped: true })
    expect(calls).toBe(1)
    gate.resume()
    await expect(gate.run()).resolves.toEqual({ stopped: false, value: 'ok' })
    expect(calls).toBe(2)
  })
  it('does not let an old rejected request stop a resumed session', async () => {
    let rejectOld!: (reason: unknown) => void
    let calls = 0
    const gate = createPollingGate(() => ++calls === 1 ? new Promise<string>((_, reject) => { rejectOld = reject }) : Promise.resolve('new session'))
    const old = gate.run().catch(() => undefined)
    gate.resume()
    rejectOld({ response: { status: 401 } })
    await old
    await expect(gate.run()).resolves.toEqual({ stopped: false, value: 'new session' })
  })
})
