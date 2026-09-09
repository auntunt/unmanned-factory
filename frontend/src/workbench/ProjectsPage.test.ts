import { describe, expect, it } from 'vitest'

import { availableProjectCandidates, projectImportNotice, validateProjectZip } from './ProjectsPage'

describe('服务器工程候选', () => {
  it('隐藏已经登记的工程，只允许连接未登记候选', () => {
    expect(availableProjectCandidates([
      { id: 'a', name: '已登记', repository: 'a/a', base_branch: 'main', registered: true, project_id: 'p1' },
      { id: 'b', name: '可登记', repository: 'b/b', base_branch: 'trunk', registered: false },
    ])).toEqual([{ id: 'b', name: '可登记', repository: 'b/b', base_branch: 'trunk', registered: false }])
  })

})


describe('项目压缩包导入', () => {
  it('拒绝缺失、非 ZIP、空文件和超过服务端上限的文件', () => {
    expect(validateProjectZip(null)).toBeTruthy()
    expect(validateProjectZip({ name: 'project.tar', size: 10 })).toBeTruthy()
    expect(validateProjectZip({ name: 'project.zip', size: 0 })).toBeTruthy()
    expect(validateProjectZip({ name: 'project.zip', size: 20 * 1024 * 1024 + 1 })).toBeTruthy()
    expect(validateProjectZip({ name: 'project.ZIP', size: 20 * 1024 * 1024 })).toBeNull()
  })
  it('导入结果明确区分文件识别和实际运行验证，并显示服务端提醒', () => {
    const notice = projectImportNotice({ filename: 'agent.zip', file_count: 12, manifests: ['package.json'], warnings: ['未发现入口'], baseline_status: 'not_run' })
    expect(notice).toContain('12 个文件')
    expect(notice).toContain('尚未运行项目')
    expect(notice).toContain('未发现入口')
  })
})
