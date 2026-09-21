// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import ClarificationPanel from './ClarificationPanel'
import { request } from '../workspace/api'

vi.mock('../workspace/api', async original => ({
  ...await original<typeof import('../workspace/api')>(),
  request: vi.fn(),
}))
const api = vi.mocked(request)
afterEach(() => { cleanup(); api.mockReset() })

const QUESTIONS = ['金额是按含税还是不含税口径？', '旧接口要保留多久？']

function renderPanel(over: Partial<React.ComponentProps<typeof ClarificationPanel>> = {}) {
  const onAnswered = vi.fn()
  render(
    <ClarificationPanel
      questions={QUESTIONS}
      endpoint="/api/v2/maintenance/tasks/t1/clarify"
      csrfToken="csrf"
      onUnauthorized={vi.fn()}
      onAnswered={onAnswered}
      {...over}
    />,
  )
  return { onAnswered }
}

describe('澄清问答面板', () => {
  it('把服务端给的问题原样列出来，并说明这不是「继续执行」', () => {
    renderPanel()
    const list = screen.getByTestId('clarification-questions')
    for (const q of QUESTIONS) expect(list.textContent).toContain(q)
    expect(screen.getByTestId('clarification-panel').textContent)
      .toContain('这一步不是「继续执行」')
  })

  it('没有问题时整块不渲染', () => {
    renderPanel({ questions: [] })
    expect(screen.queryByTestId('clarification-panel')).toBeNull()
  })

  it('空答案不提交，也不发请求', async () => {
    renderPanel()
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })
    expect(api).not.toHaveBeenCalled()
    expect(screen.getByText(/回答不能为空/)).toBeTruthy()

    // 只有空白也不行
    fireEvent.change(screen.getByTestId('clarification-answer'), { target: { value: '   ' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })
    expect(api).not.toHaveBeenCalled()
  })

  it('提交成功后把服务端读回的真实状态交给调用方，并清空输入', async () => {
    const updated = { task_id: 't1', status: 'running', pending_questions: [] }
    api.mockResolvedValue(updated)
    const { onAnswered } = renderPanel()
    const box = screen.getByTestId('clarification-answer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '  按不含税口径  ' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })

    await waitFor(() => expect(onAnswered).toHaveBeenCalledWith(updated))
    const [path, options] = api.mock.calls[0]
    expect(String(path)).toBe('/api/v2/maintenance/tasks/t1/clarify')
    expect(options?.method).toBe('POST')
    // 两端空白去掉再送，和后端的判空口径一致
    expect(options?.body).toEqual({ answer: '按不含税口径' })
    expect(box.value).toBe('')
  })

  it('提交失败时保留已经写好的答案并显示原因', async () => {
    const { WorkspaceApiError } = await vi.importActual<typeof import('../workspace/api')>('../workspace/api')
    api.mockRejectedValue(new WorkspaceApiError(409, '自动化运维已停用，历史任务不会自动继续执行'))
    const { onAnswered } = renderPanel()
    const box = screen.getByTestId('clarification-answer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '按不含税口径' } })
    await act(async () => { fireEvent.click(screen.getByTestId('clarification-submit')) })

    await waitFor(() => expect(screen.getByText(/已停用/)).toBeTruthy())
    expect(onAnswered).not.toHaveBeenCalled()
    // 输入没有被清掉——否则用户得重写一遍才能重试
    expect(box.value).toBe('按不含税口径')
    expect((screen.getByTestId('clarification-submit') as HTMLButtonElement).disabled).toBe(false)
  })

  it('提交过程中按钮禁用，连点也只发一次请求', async () => {
    let release: (value: unknown) => void = () => {}
    api.mockImplementation(() => new Promise(resolve => { release = resolve }))
    renderPanel()
    fireEvent.change(screen.getByTestId('clarification-answer'), { target: { value: '答案' } })
    const button = screen.getByTestId('clarification-submit') as HTMLButtonElement
    await act(async () => { fireEvent.click(button) })
    expect(button.disabled).toBe(true)
    expect(button.textContent).toContain('提交中')
    await act(async () => { fireEvent.click(button); fireEvent.click(button) })
    expect(api).toHaveBeenCalledTimes(1)
    await act(async () => { release({ pending_questions: [] }) })
  })

  it('不可回答时（例如插件已停用）输入与按钮都禁用', () => {
    renderPanel({ disabled: true })
    expect((screen.getByTestId('clarification-answer') as HTMLTextAreaElement).disabled).toBe(true)
    expect((screen.getByTestId('clarification-submit') as HTMLButtonElement).disabled).toBe(true)
  })
})
