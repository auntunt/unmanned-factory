import { describe, expect, it } from 'vitest'
import { githubSourceUrl, graphEdgeIsRenderable, markdownBundle, parseImportBundle } from './project-agent-types'

describe('项目代理纯辅助函数', () => {
  it('解析显式 JSON 交换包并保留文本数据', () => {
    const bundle = parseImportBundle('{"repository":"acme/app","documents":[{"path":"teamwiki/a.md","content":"# 事实"}]}')
    expect(bundle.repository).toBe('acme/app')
    expect(bundle.documents[0].content).toBe('# 事实')
  })

  it('拒绝缺少 repository 或 documents 的交换包', () => {
    expect(() => parseImportBundle('{"documents":[]}')).toThrow('repository')
    expect(() => parseImportBundle('{"repository":"acme/app","documents":[]}')).toThrow('至少包含一项')
  })

  it('选中文件只组装为 teamwiki 数据路径，不读取任意本地路径', () => {
    const bundle = markdownBundle('acme/app', [{ name: '/private/guide.md', content: 'safe' }])
    expect(bundle.documents[0].path).toBe('teamwiki/guide.md')
    expect(bundle.documents[0].content).toBe('safe')
  })

  it('只绘制两端都存在的真实边', () => {
    expect(graphEdgeIsRenderable({ source: 'a', target: 'b', kind: 'calls', resolution: 'syntax', path: 'a.py', line: 1 }, new Set(['a', 'b']))).toBe(true)
    expect(graphEdgeIsRenderable({ source: 'a', target: 'missing', kind: 'calls', resolution: 'heuristic', path: 'a.py', line: 1 }, new Set(['a', 'b']))).toBe(false)
  })

  it('在前端先限制 TeamAI 交换包大小', () => {
    const tooLong = 'x'.repeat(16001)
    expect(() => parseImportBundle(JSON.stringify({ repository: 'acme/app', documents: [{ path: 'teamwiki/a.md', content: tooLong }] }))).toThrow('16000')
    expect(() => markdownBundle('acme/app', Array.from({ length: 21 }, (_, index) => ({ name: `${index}.md`, content: 'ok' })))).toThrow('20')
  })

  it('生成固定 SHA 的 GitHub 源码链接并编码路径段', () => {
    const sha = '0123456789abcdef0123456789abcdef01234567'
    expect(githubSourceUrl('acme/app', sha, 'src/hello world.ts', 12)).toBe('https://github.com/acme/app/blob/0123456789abcdef0123456789abcdef01234567/src/hello%20world.ts?plain=1#L12')
  })

  it('拒绝不安全或不具体的 GitHub 源码定位参数', () => {
    const sha = '0123456789abcdef0123456789abcdef01234567'
    expect(githubSourceUrl('acme', sha, 'src/app.ts', 1)).toBeNull()
    expect(githubSourceUrl('acme/app', 'not-a-sha', 'src/app.ts', 1)).toBeNull()
    expect(githubSourceUrl('acme/app', sha, '../secret', 1)).toBeNull()
    expect(githubSourceUrl('acme/app', sha, 'src\\app.ts', 1)).toBeNull()
    expect(githubSourceUrl('acme/app', sha, 'src/app.ts', 0)).toBeNull()
  })
})
