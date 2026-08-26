// 执行现场查看器：transcript 正文 / 代码 diff。
//
// 这是「看板」变「工作台」的核心 —— 在此之前 attempt 展开只有三行监工结论，
// 人想知道 AI 到底干了什么只能 ssh 上去翻 /tmp（而那些文件随时被清）。
//
// 设计取舍：**不解析 transcript 格式**。它是 claude 输出的 JSONL，
// 按工具调用折叠看起来更漂亮，但 claude 换一版输出格式就全瞎。原始正文
// 永远读得出来，所以这里只做一件事：等宽、可搜、行号，像 less 一样。

import { useEffect, useState } from 'react'
import type { AttemptArtifact } from '../types'
import { ApiError, fetchArtifact } from '../lib/api'
import {
  classifyLine,
  emptyReason,
  filterLines,
  fmtBytes,
  toLines,
} from '../lib/artifactFormat'
import type { ArtifactKind, LineKind } from '../lib/artifactFormat'

type Kind = ArtifactKind

const KIND_CN: Record<Kind, string> = {
  transcript: '执行日志',
  diff: '代码改动',
}

export default function ArtifactViewer({
  taskId,
  attemptNo,
  kind,
}: {
  taskId: string
  attemptNo: number
  kind: Kind
}) {
  const [art, setArt] = useState<AttemptArtifact | null>(null)
  const [err, setErr] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')

  useEffect(() => {
    let dead = false
    setLoading(true)
    setErr(null)
    fetchArtifact(taskId, attemptNo, kind)
      .then((got) => {
        if (!dead) setArt(got)
      })
      .catch((e) => {
        if (!dead) setErr(e instanceof ApiError ? e : new ApiError(0, { error: String(e) }))
      })
      .finally(() => {
        if (!dead) setLoading(false)
      })
    return () => {
      dead = true
    }
  }, [taskId, attemptNo, kind])

  if (loading) {
    return (
      <p className="px-2 py-3 font-mono text-xs text-slate-500">
        [ loading ] 正在取{KIND_CN[kind]}…
      </p>
    )
  }

  if (err) {
    return (
      <div className="px-2 py-3 font-mono text-xs">
        <div className="text-rose-700">[ error ] {err.message}</div>
        {err.hint && <div className="mt-1 text-slate-600">→ {err.hint}</div>}
      </div>
    )
  }

  if (!art) return null

  // note 非空 = 后端给不出完整正文，必须显示原因。
  // 「文件随 /tmp 丢了」和「这一轮没改代码」在界面上都是空白，
  // 但一个是数据事故一个是正常状态。
  const lines = toLines(art.body)
  const shown = filterLines(lines, filter)

  return (
    <div className="font-mono text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-200 bg-slate-100 px-2 py-1 text-[10px]">
        <span className="font-semibold uppercase tracking-wider text-slate-600">
          {kind}
        </span>
        <span className="text-slate-500 tabular-nums">
          {lines.length} 行 · {fmtBytes(art.bytes)}
        </span>
        {lines.length > 0 && (
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="/ 过滤行"
            aria-label={`过滤${KIND_CN[kind]}`}
            className="ml-auto w-32 rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-800 placeholder:text-slate-400"
          />
        )}
        {filter && (
          <span className="text-sky-700 tabular-nums">
            {shown.length}/{lines.length} 命中
          </span>
        )}
      </div>

      {art.note && (
        <div className="border-b border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
          ⚠ {art.note}
        </div>
      )}

      {lines.length === 0 ? (
        <p className="px-2 py-3 text-slate-500">
          [ empty ] {emptyReason(kind, art.note ?? '')}
        </p>
      ) : shown.length === 0 ? (
        <p className="px-2 py-3 text-slate-500">
          [ filtered ] {lines.length} 行里没有匹配「{filter}」的。
        </p>
      ) : (
        <pre className="max-h-96 overflow-auto bg-white px-2 py-1.5 leading-relaxed">
          {shown.map((line, i) => (
            <div key={i} className={lineClass(line, kind)}>
              {line || '\u00a0'}
            </div>
          ))}
        </pre>
      )}

      {art.path && (
        <div className="border-t border-slate-200 bg-slate-50 px-2 py-1 text-[10px] text-slate-400 break-all">
          {art.path}
        </div>
      )}
    </div>
  )
}

/** 行语义 → tailwind 类。分类逻辑在 lib/artifactFormat（那里有测试兜着）。 */
const LINE_CLASS: Record<LineKind, string> = {
  meta: 'text-slate-500 font-semibold',
  hunk: 'text-sky-700',
  add: 'bg-emerald-50 text-emerald-800',
  del: 'bg-rose-50 text-rose-800',
  plain: 'text-slate-600',
}

function lineClass(line: string, kind: Kind): string {
  const base = 'whitespace-pre-wrap break-all'
  // transcript 一律深灰：JSONL 里 `-` 开头的内容多得是，
  // 按 diff 规则上色会满屏乱红。
  if (kind !== 'diff') return `${base} text-slate-700`
  return `${base} ${LINE_CLASS[classifyLine(line, kind)]}`
}
