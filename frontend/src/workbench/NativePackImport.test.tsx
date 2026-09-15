// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { request, WorkspaceApiError } from '../workspace/api'
import NativePackImport from './NativePackImport'
vi.mock('../workspace/api', async original => ({ ...await original<typeof import('../workspace/api')>(), request: vi.fn() }))
const props = { csrfToken: 'csrf', onUnauthorized: vi.fn(), onImported: vi.fn() }
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('keeps native restoration secondary and explains an external ZIP rejection without bypassing review', async () => {
  vi.mocked(request).mockRejectedValue(new WorkspaceApiError(400, '外部 skill ZIP 请使用“导入外部 skill 包”'))
  render(<NativePackImport {...props} />)
  expect(screen.getByRole('region', { name: '恢复职能包' })).toBeTruthy()
  const input = screen.getByLabelText('选择 webuddy 职能包 ZIP') as HTMLInputElement
  fireEvent.change(input, { target: { files: [new File(['zip'], 'reverse-skill.zip')] } })
  expect((await screen.findByRole('alert')).textContent).toContain('外部 skill ZIP 请使用')
  expect(props.onImported).not.toHaveBeenCalled()
  expect(vi.mocked(request).mock.calls.map(([path]) => path)).toEqual(['/api/v4/agent-packs/import'])
  await waitFor(() => expect(input.disabled).toBe(false))
  expect(input.value).toBe('')
})
it('still restores a native pack with multipart and CSRF', async () => {
  vi.mocked(request).mockResolvedValue({ id: 'agent' })
  render(<NativePackImport {...props} />)
  fireEvent.change(screen.getByLabelText('选择 webuddy 职能包 ZIP'), { target: { files: [new File(['zip'], 'native.zip')] } })
  await waitFor(() => expect(props.onImported).toHaveBeenCalledWith('agent'))
  expect(request).toHaveBeenCalledWith('/api/v4/agent-packs/import', expect.objectContaining({ method: 'POST', csrfToken: 'csrf', body: expect.any(FormData) }))
})
