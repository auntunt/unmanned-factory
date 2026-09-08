import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { RunKnowledgeView } from './RunKnowledge'

describe('RunKnowledgeView contract rendering', () => {
  it('renders the frozen commit SHA fields and nested merge evidence', () => {
    const html = renderToStaticMarkup(<RunKnowledgeView
      status="published"
      context={{ commit_sha: 'base-sha', code: { commit_sha: 'index-sha' }, warnings: ['索引过期'] }}
      artifacts={{ pr_url: 'https://github.com/acme/app/pull/7' }}
      loading={false}
      error={null}
      mergeBusy={false}
      mergeResult={{ merged: true, evidence: { merge_commit_sha: 'merge-sha' } }}
      onSyncMerge={vi.fn()}
    />)
    expect(html).toContain('base-sha')
    expect(html).toContain('index-sha')
    expect(html).toContain('merge-sha')
    expect(html).toContain('核验合并状态')
  })

  it('does not show merge confirmation without a stored PR URL', () => {
    const html = renderToStaticMarkup(<RunKnowledgeView
      status="running"
      context={null}
      artifacts={null}
      loading={false}
      error={null}
      mergeBusy={false}
      mergeResult={{ merged: false, reason: '尚未合并' }}
      onSyncMerge={vi.fn()}
    />)
    expect(html).not.toContain('核验合并状态')
    expect(html).toContain('尚未合并')
  })
})
