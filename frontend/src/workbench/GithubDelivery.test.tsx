import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import GithubDelivery, { githubPublicationLabel, githubPublishBody, safeRepositoryUrl, type GithubOptions } from './GithubDelivery'
import type { Run } from '../workspace/types'
const run = { id: 'r1', project_id: 'p1', request: '工具', revision: 1, status: 'ready_for_review', created_at: '', updated_at: '' } as Run
const options: GithubOptions = { github_configured: true, github_repository_bound: false, project_revision: 4, repositories: [], page: 1, has_more: false }

describe('GitHub 成果发布', () => {
  it('区分缺凭据、未绑定和首次上传，首次上传不声称创建PR', () => {
    expect(githubPublicationLabel(run, false)).toBe('未配置发布凭据')
    expect(githubPublicationLabel(run, true, false)).toBe('尚未绑定仓库')
    expect(githubPublicationLabel({ ...run, status: 'published', artifacts: { publication_type: 'initial', published_branch: 'trunk' } }, true)).toBe('已上传到仓库 trunk 分支')
    expect(githubPublicationLabel({ ...run, status: 'published', artifacts: { pr_url: 'https://github.com/a/b/pull/1' } }, true)).toBe('已创建 PR')
  })
  it('创建请求固定私有并携带项目版本，已有仓库请求不混入创建参数', () => {
    expect(githubPublishBody('create', options, '', ' work-hours ')).toEqual({ mode: 'create', name: 'work-hours', private: true, expected_project_revision: 4 })
    expect(githubPublishBody('existing', options, 'team/work-hours', '')).toEqual({ mode: 'existing', repository: 'team/work-hours', expected_project_revision: 4 })
    expect(() => githubPublishBody('existing', options, '', '')).toThrow('请选择')
    expect(() => githubPublishBody('create', options, '', '../bad/name')).toThrow('仓库名称')
  })
  it('成员不出现操作按钮，管理员可打开上传面板，凭据无需输入', () => {
    const props = { run, csrfToken: 'csrf', onUnauthorized: () => undefined, configured: true }
    const member = renderToStaticMarkup(<GithubDelivery {...props} isAdmin={false} />)
    expect(member).toContain('请管理员')
    expect(member).not.toContain('<button')
    const admin = renderToStaticMarkup(<GithubDelivery {...props} isAdmin />)
    expect(admin).toContain('上传到 GitHub')
    expect(admin).not.toContain('type="password"')
  })
  it('首次发布展示实际仓库链接并拒绝非GitHub链接', () => {
    const html = renderToStaticMarkup(<GithubDelivery run={{ ...run, status: 'published', artifacts: { publication_type: 'initial', published_branch: 'trunk', repository_url: 'https://github.com/team/tool' } }} csrfToken="" onUnauthorized={() => undefined} configured isAdmin />)
    expect(html).toContain('https://github.com/team/tool')
    expect(html).toContain('trunk')
    expect(html).not.toContain('main')
    expect(html).not.toContain('PR')
    expect(safeRepositoryUrl('javascript:alert(1)')).toBeNull()
    expect(safeRepositoryUrl('https://github.com.evil.test/team/tool')).toBeNull()
  })
})


it('仓库名称拒绝非法首字符，仓库链接拒绝凭据和显式端口', () => {
  for (const name of ['.hidden', '-leading', '_leading']) expect(() => githubPublishBody('create', options, '', name)).toThrow('仓库名称')
  for (const url of ['https://user:secret@github.com/team/tool', 'https://github.com:8443/team/tool', 'https://github.com:443/team/tool']) expect(safeRepositoryUrl(url)).toBeNull()
  expect(githubPublicationLabel({ ...run, status: 'published', artifacts: { publication_type: 'initial' } }, true)).toBe('已首次上传到仓库')
})
