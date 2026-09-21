import { useState } from 'react'
import type { FormEvent } from 'react'

import { request, WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, errorText } from './ui'

/** 模型在动手前提出的问题，以及回答它的入口。
 *
 *  三个业务场景（维护、信创、适配）共用这一个组件：后端契约是同一套
 *  （`pending_questions` + `POST .../clarify` 收 `{answer}`），所以界面也只做一套，
 *  不让三个页面各长一个样。
 *
 *  两件刻意的事：
 *  - **回答不是「继续执行」。** 停在提问上的运行需要的是答案；resume 只会把它
 *    推回同一个闸门。所以这里只提供「提交回答」，调用方也不要拿 resume 顶替。
 *  - **失败不清空输入。** 提交失败时原样保留已经写好的答案并显示原因，
 *    否则用户要重写一遍才能重试。成功之后由调用方用服务端读回的真实状态刷新，
 *    不做乐观更新。 */
export interface ClarificationPanelProps<T> {
  questions: string[]
  /** 形如 `/api/v2/maintenance/tasks/<id>/clarify` */
  endpoint: string
  csrfToken?: string
  onUnauthorized?: () => void
  /** 服务端读回的最新任务视图 */
  onAnswered: (updated: T) => void
  /** 停用插件等原因导致不可回答时置为 true；不在前端另造权限判断 */
  disabled?: boolean
}

export default function ClarificationPanel<T>({
  questions, endpoint, csrfToken, onUnauthorized, onAnswered, disabled,
}: ClarificationPanelProps<T>) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (questions.length === 0) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!answer.trim()) {
      setError('回答不能为空。')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const updated = await request<T>(endpoint, {
        method: 'POST', csrfToken, onUnauthorized, body: { answer: answer.trim() },
      })
      setAnswer('')
      onAnswered(updated)
    } catch (cause) {
      if (cause instanceof WorkspaceApiError && cause.status === 401) return
      // 输入原样留着，用户不用重写一遍。
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="wb-card" data-testid="clarification-panel">
      <h3>模型提出了需要确认的问题</h3>
      <p className="wb-muted">
        回答之后会重新规划；这一步不是「继续执行」——停在提问上的运行需要的是答案。
      </p>
      <ol className="wb-plain-list" data-testid="clarification-questions">
        {questions.map((question, index) => (
          <li key={index}>{question}</li>
        ))}
      </ol>
      <form onSubmit={submit}>
        <label>
          你的回答
          <textarea
            value={answer}
            rows={4}
            disabled={busy || disabled}
            placeholder="逐条回答上面的问题；写得越具体，重新规划越不容易再问一遍。"
            onChange={event => setAnswer(event.target.value)}
            data-testid="clarification-answer"
          />
        </label>
        {error && <ErrorNotice message={error} />}
        <div className="wb-form-actions">
          <button
            type="submit"
            className="wb-button wb-button-primary"
            disabled={busy || disabled}
            data-testid="clarification-submit"
          >
            {busy ? '提交中…' : '提交回答'}
          </button>
        </div>
      </form>
    </section>
  )
}
