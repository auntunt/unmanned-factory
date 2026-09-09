export class WorkspaceApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'WorkspaceApiError'
    this.status = status
    this.detail = detail
  }
}

export interface RequestOptions {
  method?: string
  body?: unknown
  csrfToken?: string
  onUnauthorized?: () => void
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  const multipart = options.body instanceof FormData
  if (options.body !== undefined && !multipart) headers['Content-Type'] = 'application/json'
  if (options.csrfToken && method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = options.csrfToken

  let response: Response
  try {
    response = await fetch(path, {
      method,
      cache: method === 'GET' || method === 'HEAD' ? 'no-store' : undefined,
      credentials: 'same-origin',
      headers,
      body: options.body === undefined ? undefined : multipart ? options.body as FormData : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new WorkspaceApiError(0, error instanceof Error ? error.message : '网络请求失败')
  }

  if (response.status === 401) {
    options.onUnauthorized?.()
    throw new WorkspaceApiError(401, '登录已过期，请重新登录')
  }

  const raw = await response.text()
  let payload: unknown = {}
  if (raw) {
    try {
      payload = JSON.parse(raw)
    } catch {
      payload = { detail: raw.slice(0, 300) }
    }
  }
  if (!response.ok) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload
      ? String(payload.detail)
      : `请求失败（HTTP ${response.status}）`
    throw new WorkspaceApiError(response.status, detail)
  }
  return payload as T
}
