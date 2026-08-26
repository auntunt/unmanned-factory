// 现场正文的纯展示逻辑。
//
// 从 ArtifactViewer 里抽出来是为了能测 —— 项目里没装 jsdom/testing-library，
// 组件渲染测不了，但这几个判断恰好是最容易出错的地方（diff 的 `---` 前缀
// 和删除行的 `-` 前缀撞车，顺序写反整个文件头会被标成删除行）。

export type ArtifactKind = 'transcript' | 'diff'

/** diff 行的语义分类。transcript 一律 'plain'。 */
export type LineKind = 'meta' | 'hunk' | 'add' | 'del' | 'plain'

/**
 * 判断 diff 行的类型。
 *
 * 顺序有讲究：`---`/`+++` 必须在 `-`/`+` **之前**判，否则 unified diff 的
 * 文件头会被当成删除行/新增行涂成红绿，一屏 diff 顶上永远挂两条假的增删。
 */
export function classifyLine(line: string, kind: ArtifactKind): LineKind {
  if (kind !== 'diff') return 'plain'
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return 'plain'
}

/** 人可读的字节数。 */
export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  return `${(n / 1024 / 1024).toFixed(1)}MB`
}

/**
 * 切行。
 *
 * 空正文返回空数组而不是 `['']` —— 后者会让界面显示「1 行」，
 * 而「这一轮没有代码改动」和「有一行空行」是不同的事实。
 */
export function toLines(body: string): string[] {
  return body ? body.split('\n') : []
}

/** 大小写不敏感的行过滤。空 filter 原样返回（不拷贝，调用方不改）。 */
export function filterLines(lines: string[], filter: string): string[] {
  if (!filter) return lines
  const needle = filter.toLowerCase()
  return lines.filter((l) => l.toLowerCase().includes(needle))
}

/**
 * 正文为空时该说什么。
 *
 * 三种空是三件不同的事，不能都显示「暂无数据」：
 *   - note 非空：后端明确解释了为什么给不出（文件丢了 / 非 claude harness）
 *   - diff 空：正常状态，只读操作或全被权限拦下
 *   - transcript 空：不正常，派过工就该有日志
 */
export function emptyReason(kind: ArtifactKind, note: string): string {
  if (note) return '见上方说明。'
  if (kind === 'diff') return '这一轮没有代码改动（只读操作，或全部被权限拦下）。'
  return '没有日志正文。'
}
