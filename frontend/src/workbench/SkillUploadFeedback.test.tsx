// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SkillUploadFeedback, { uploadSkillAttachment } from './SkillUploadFeedback'
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it.each(['Skill 文件数超过 500；请改用外部 skill 包·适配与人签', 'Skill 单文件超过 2 MiB'])('preserves backend upload reason: %s', async message => {
 const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: message }), { status: 400 }))
 vi.stubGlobal('fetch', fetch)
 await expect(uploadSkillAttachment('/api/v4/agents/a/skills', new File(['zip'],'large.zip'), {csrfToken:'csrf',onUnauthorized:vi.fn()})).rejects.toThrow(message)
 expect(fetch.mock.calls[0][1].body).toBeInstanceOf(FormData)
 expect(fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBe('csrf')
 render(<MemoryRouter><SkillUploadFeedback message={message}/></MemoryRouter>)
 expect(screen.getByRole('alert').textContent).toContain(message)
 if (message.includes('外部 skill 包')) expect(screen.getByRole('link').getAttribute('href')).toBe('/agents?import=external')
 else expect(screen.queryByRole('link')).toBeNull()
})
