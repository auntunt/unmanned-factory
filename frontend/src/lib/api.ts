// 人工介入动作的调用层。
//
// 为什么单独一个文件：这些是**写**请求，和散在各页面的 axios.get 不是一类东西。
// 写请求需要统一处理三件事，重复三遍必然漏一遍：
//   1. 后端的错误体是 {error, hint?, allowed?}，不是 HTTP 状态码就够了 ——
//      409「已经不在 needs-human 了」和 400「理由太长」要给人完全不同的话。
//   2. 连点保护要在调用层，不能靠每个按钮各自记 disabled。
//   3. 失败后必须刷新数据：写请求失败往往意味着服务端状态已经变了
//      （别人抢先处理了这个任务），继续显示旧状态会让人重复点。

import type { AttemptArtifact } from '../types'

/** 后端 4xx/5xx 的响应体形状。 */
export interface ApiErrorBody {
  error: string
  /** 该怎么办。后端在状态冲突时给，直接显示给人看。 */
  hint?: string
  /** 枚举类参数错误时的可选值。 */
  allowed?: string[]
  /** rerun 半成功时：定案落了但队列没搬。 */
  audit_done?: boolean
}

/**
 * 带上后端解释的错误。
 *
 * 不用裸 Error：`hint` 是后端专门写给操作者看的下一步，丢掉它等于把
 * 「定案已生效，队列问题解决后再点一次」退化成一个红色感叹号。
 */
export class ApiError extends Error {
  readonly status: number
  readonly hint?: string
  readonly allowed?: string[]
  readonly auditDone?: boolean

  constructor(status: number, body: ApiErrorBody) {
    super(body.error || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.hint = body.hint
    this.allowed = body.allowed
    this.auditDone = body.audit_done
  }
}

async function post<T>(path: string, payload: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  } catch (err) {
    // 网络层失败（服务挂了、反代 502、断网）。这时**完全不知道**请求有没有
    // 到服务端，所以话不能说成「操作失败」—— 可能已经生效了。
    throw new ApiError(0, {
      error: `请求发不出去：${err instanceof Error ? err.message : String(err)}`,
      hint: '不确定服务端是否已收到。刷新页面看当前状态，不要直接重试',
    })
  }

  // 后端所有响应都是 JSON，但反代出错时可能塞 HTML 进来。
  let body: unknown
  const raw = await res.text()
  try {
    body = raw ? JSON.parse(raw) : {}
  } catch {
    throw new ApiError(res.status, {
      error: `服务端返回的不是 JSON（HTTP ${res.status}）`,
      hint: raw.slice(0, 200) || '响应体是空的',
    })
  }

  if (!res.ok) throw new ApiError(res.status, body as ApiErrorBody)
  return body as T
}

/** 写动作的成功响应。字段随动作不同，message 一定有。 */
export interface ActionResult {
  ok: true
  task_id: string
  /** 后端组织好的中文说明，直接显示 —— 它知道实际落到了第几轮。 */
  message: string
  attempt_no?: number
  resolution?: string
  verdict?: string
  note?: string
  from_state?: string
}

/** 定案：落审计，不动队列。 */
export function resolveTask(
  taskId: string,
  resolution: 'merged' | 'reworked',
  note: string,
): Promise<ActionResult> {
  return post(`/api/task/${encodeURIComponent(taskId)}/resolve`, {
    resolution,
    note,
  })
}

/** 人工验收。attemptNo 省略则落在最后一轮。 */
export function acceptTask(
  taskId: string,
  verdict: 'pass' | 'fail',
  note: string,
  attemptNo?: number,
): Promise<ActionResult> {
  return post(`/api/task/${encodeURIComponent(taskId)}/accept`, {
    verdict,
    note,
    attempt_no: attemptNo,
  })
}

/** 定案 reworked 并放回 inbox。定案词固定，不给调用方选。 */
export function rerunTask(taskId: string, note: string): Promise<ActionResult> {
  return post(`/api/task/${encodeURIComponent(taskId)}/rerun`, { note })
}

/**
 * 取一轮的执行现场正文。
 *
 * 不做缓存：transcript 一份 150KB，缓存三轮就是半兆挂在内存里，而人看完
 * 一轮基本不回头。真需要的话在组件里存 state 就够。
 */
export async function fetchArtifact(
  taskId: string,
  attemptNo: number,
  kind: 'transcript' | 'diff',
): Promise<AttemptArtifact> {
  const url = `/api/task/${encodeURIComponent(taskId)}/attempt/${attemptNo}/${kind}`
  const res = await fetch(url)
  const raw = await res.text()
  let body: unknown
  try {
    body = raw ? JSON.parse(raw) : {}
  } catch {
    throw new ApiError(res.status, {
      error: `现场正文不是 JSON（HTTP ${res.status}）`,
    })
  }
  if (!res.ok) throw new ApiError(res.status, body as ApiErrorBody)
  return body as AttemptArtifact
}
