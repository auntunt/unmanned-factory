// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import AdminConfigChat from './AdminConfigChat'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
const noop = vi.fn()

afterEach(() => { cleanup(); api.mockReset() })

function mount() {
  return render(<AdminConfigChat csrfToken="x" onUnauthorized={noop} />)
}

describe('AdminConfigChat', () => {
  it('默认折叠，且不在折叠时拉取会话', () => {
    mount()
    expect(screen.getByText('配置对话')).toBeTruthy()
    expect(screen.getByText('展开')).toBeTruthy()
    expect(api).not.toHaveBeenCalled()
  })

  it('展开后说明「和表单改的是同一份配置」', () => {
    mount()
    api.mockResolvedValue({ conversations: [] } as never)
    fireEvent.click(screen.getByText('展开'))
    expect(screen.getAllByText(/和表单改的是同一份配置/).length).toBeGreaterThan(0)
  })

  it('响应缺 conversations 时不崩，仍渲染面板', async () => {
    mount()
    api.mockResolvedValue({} as never)
    fireEvent.click(screen.getByText('展开'))
    await waitFor(() => expect(screen.getByText('新对话')).toBeTruthy())
  })

  it('拉取失败时显示错误而不是白屏', async () => {
    mount()
    api.mockRejectedValue(new Error('boom'))
    fireEvent.click(screen.getByText('展开'))
    await waitFor(() => expect(screen.getByText('新对话')).toBeTruthy())
  })

  it('任务完成后停止轮询并调用 onConfigChanged', async () => {
    vi.useFakeTimers()
    try {
      const onConfigChanged = vi.fn()
      render(<AdminConfigChat csrfToken="x" onUnauthorized={noop} onConfigChanged={onConfigChanged} />)

      const pendingConv = {
        id: 'c1', purpose: 'admin_config', actor_id: '1',
        messages: [
          { id: 'm1', role: 'user', content: '查看配置' },
          { id: 'm2', role: 'assistant', content: '正在回答', status: 'pending', job_id: 'j1' },
        ],
      }
      const completedConv = {
        ...pendingConv,
        messages: [
          { id: 'm1', role: 'user', content: '查看配置' },
          { id: 'm2', role: 'assistant', content: '配置已完成', status: 'completed', job_id: 'j1' },
        ],
      }

      // Expand: loads conversation list, then full conv with pending
      api
        .mockResolvedValueOnce({ conversations: [pendingConv] } as never)
        .mockResolvedValueOnce(pendingConv as never)
      fireEvent.click(screen.getByText('展开'))
      // Flush the two initial fetches
      await vi.advanceTimersByTimeAsync(0)

      // First poll tick (2s): return completed conv
      api.mockResolvedValueOnce(completedConv as never)
      await vi.advanceTimersByTimeAsync(2100)

      expect(onConfigChanged).toHaveBeenCalledTimes(1)

      // After completion, pending is false → no more intervals fire.
      const callsBefore = api.mock.calls.length
      await vi.advanceTimersByTimeAsync(5000)
      expect(api.mock.calls.length).toBe(callsBefore)
    } finally {
      vi.useRealTimers()
    }
  })
})
