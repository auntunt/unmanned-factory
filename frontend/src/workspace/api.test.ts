import { afterEach, describe, expect, it, vi } from 'vitest'
import { request, WorkspaceApiError } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('上传项目请求', () => {
  it('保留 multipart body 和浏览器边界，同时发送登录凭证及 CSRF', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ project: { id: 'p1' } }), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    const body = new FormData()
    body.append('name', '转换器')
    body.append('file', new Blob(['zip']), 'project.zip')
    expect(await request('/api/v2/projects/import-zip', { method: 'POST', csrfToken: 'csrf', body })).toEqual({ project: { id: 'p1' } })
    const options = fetcher.mock.calls[0][1]
    expect(options.body).toBe(body)
    expect(options.credentials).toBe('same-origin')
    expect(options.headers['X-CSRF-Token']).toBe('csrf')
    expect(options.headers['Content-Type']).toBeUndefined()
  })
  it('保留普通 JSON 请求格式', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    await request('/api/v2/projects/create-workspace', { method: 'POST', body: { name: '项目' } })
    expect(fetcher.mock.calls[0][1].body).toBe('{"name":"项目"}')
    expect(fetcher.mock.calls[0][1].headers['Content-Type']).toBe('application/json')
  })
  it('将服务端压缩包校验错误提供给表单展示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"压缩包包含不安全路径"}', { status: 422 })))
    await expect(request('/api/v2/projects/import-zip', { method: 'POST', body: new FormData() })).rejects.toEqual(new WorkspaceApiError(422, '压缩包包含不安全路径'))
  })
})
