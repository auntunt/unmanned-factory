import { describe, expect, it } from 'vitest'

import { availableProjectCandidates } from './ProjectsPage'

describe('服务器工程候选', () => {
  it('隐藏已经登记的工程，只允许连接未登记候选', () => {
    expect(availableProjectCandidates([
      { id: 'a', name: '已登记', repository: 'a/a', base_branch: 'main', registered: true, project_id: 'p1' },
      { id: 'b', name: '可登记', repository: 'b/b', base_branch: 'trunk', registered: false },
    ])).toEqual([{ id: 'b', name: '可登记', repository: 'b/b', base_branch: 'trunk', registered: false }])
  })

})
