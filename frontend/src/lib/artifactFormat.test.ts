import { describe, expect, it } from 'vitest'
import {
  classifyLine,
  emptyReason,
  filterLines,
  fmtBytes,
  toLines,
} from './artifactFormat'

describe('classifyLine', () => {
  it('文件头不能被当成增删行', () => {
    // 这是最容易写错的地方：`---` 也以 `-` 开头，判断顺序反了
    // 每屏 diff 顶上都会挂两条假的红绿行。
    expect(classifyLine('--- a/factory/api.py', 'diff')).toBe('meta')
    expect(classifyLine('+++ b/factory/api.py', 'diff')).toBe('meta')
  })

  it('识别 hunk 头', () => {
    expect(classifyLine('@@ -1,5 +1,9 @@ def route_post', 'diff')).toBe('hunk')
  })

  it('识别增删和上下文行', () => {
    expect(classifyLine('+    新增一行', 'diff')).toBe('add')
    expect(classifyLine('-    删掉一行', 'diff')).toBe('del')
    expect(classifyLine('     没动的一行', 'diff')).toBe('plain')
  })

  it('空行是上下文行', () => {
    expect(classifyLine('', 'diff')).toBe('plain')
  })

  it('transcript 不上色', () => {
    // JSONL 里 `-` 开头的内容多得是，按 diff 规则上色会满屏乱红。
    expect(classifyLine('- 这是日志里的一句话', 'transcript')).toBe('plain')
    expect(classifyLine('+++ 日志里恰好有这三个字符', 'transcript')).toBe('plain')
  })
})

describe('toLines', () => {
  it('空正文是零行不是一行', () => {
    // ''.split('\n') === [''] 会让界面显示「1 行」，
    // 但「没有改动」和「有一个空行」是不同的事实。
    expect(toLines('')).toEqual([])
  })

  it('末尾换行不产生幽灵行以外的行数', () => {
    expect(toLines('a\nb')).toEqual(['a', 'b'])
    expect(toLines('a\nb\n')).toEqual(['a', 'b', ''])
  })
})

describe('filterLines', () => {
  const lines = ['ERROR: 连接超时', 'info: ok', 'error: 重试']

  it('大小写不敏感', () => {
    expect(filterLines(lines, 'error')).toHaveLength(2)
    expect(filterLines(lines, 'ERROR')).toHaveLength(2)
  })

  it('匹配中文', () => {
    expect(filterLines(lines, '超时')).toEqual(['ERROR: 连接超时'])
  })

  it('空 filter 返回全部', () => {
    expect(filterLines(lines, '')).toBe(lines)
  })

  it('没命中返回空数组而不是全部', () => {
    expect(filterLines(lines, 'nonexistent')).toEqual([])
  })
})

describe('fmtBytes', () => {
  it('按量级换单位', () => {
    expect(fmtBytes(0)).toBe('0B')
    expect(fmtBytes(512)).toBe('512B')
    expect(fmtBytes(1024)).toBe('1.0KB')
    expect(fmtBytes(153600)).toBe('150.0KB')
    expect(fmtBytes(2621440)).toBe('2.5MB')
  })

  it('脏数据不显示 NaN', () => {
    expect(fmtBytes(NaN)).toBe('—')
    expect(fmtBytes(-1)).toBe('—')
  })
})

describe('emptyReason', () => {
  it('后端给了说明就让它说', () => {
    const note = '归档文件已丢失（源文件在 /tmp 被清理）'
    expect(emptyReason('diff', note)).toBe('见上方说明。')
    expect(emptyReason('transcript', note)).toBe('见上方说明。')
  })

  it('diff 空是正常状态，transcript 空不是', () => {
    // 这两句必须不一样，否则人没法区分「AI 什么都没改」和「日志没了」。
    expect(emptyReason('diff', '')).toContain('没有代码改动')
    expect(emptyReason('transcript', '')).not.toContain('没有代码改动')
  })
})
