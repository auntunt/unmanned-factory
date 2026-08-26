// 人工介入操作条：定案 / 验收 / 放回队列。
//
// 在此之前看板底部那行「factory inbox」「人工定案」是纯文字，不是按钮 ——
// CLI 有 resolve/override/defect，Web 一个都没有。这个组件把那三个动作真的接上。
//
// 三条不肯让步的规则：
//   1. 理由必填。没理由的定案在审计里等于没发生 —— 下一个人看到一个「merged」
//      除了重读全部 diff 什么都不知道。
//   2. 每个动作先说清后果再执行。「放回队列」会让任务重跑并花钱，
//      不能和「定案」长得一样、挨在一起、都是一下点完。
//   3. 失败要把后端的 hint 显示出来。409「定案已生效，队列问题解决后再点一次」
//      和 400「理由太长」需要人做完全不同的事。

import { useState } from 'react'
import { ApiError, acceptTask, rerunTask, resolveTask } from '../lib/api'
import type { ActionResult } from '../lib/api'

type Action = 'resolve_merged' | 'resolve_reworked' | 'accept_pass' | 'accept_fail' | 'rerun'

interface Spec {
  /** 按钮上的字 */
  label: string
  /** 展开后的那句「你正在做什么」 */
  what: string
  /** 理由输入框的 placeholder，引导写有用的内容 */
  hint: string
  tone: string
  run: (taskId: string, note: string, attemptNo?: number) => Promise<ActionResult>
}

const SPECS: Record<Action, Spec> = {
  resolve_merged: {
    label: '定案 merged',
    what: '判定这一轮的红是假阳性（监工误报），产出可用。不会重跑，任务留在原队列。',
    hint: '例：三条 fail 都是环境缺 pytest-xdist，跟改动无关',
    tone: 'border-emerald-300 text-emerald-800 hover:bg-emerald-50',
    run: (t, n) => resolveTask(t, 'merged', n),
  },
  resolve_reworked: {
    label: '定案 reworked',
    what: '判定这一轮的红是真问题，但**不重跑**（你要自己改，或先改判据）。',
    hint: '例：检查确实该红，判据写窄了，我手动改 checks 再说',
    tone: 'border-amber-300 text-amber-800 hover:bg-amber-50',
    run: (t, n) => resolveTask(t, 'reworked', n),
  },
  accept_pass: {
    label: '验收通过',
    what: '产出人工确认可用。这跟 resolution 是两件事 —— 它记的是「活干得好」。',
    hint: '例：读过 diff，改法合理，命名和现有代码一致',
    tone: 'border-sky-300 text-sky-800 hover:bg-sky-50',
    run: (t, n, a) => acceptTask(t, 'pass', n, a),
  },
  accept_fail: {
    label: '验收不通过',
    what: '检查可能全绿但人不认。这种组合最有价值：说明判据写窄了。',
    hint: '例：功能对了但把错误全吞了，日志里看不出失败原因',
    tone: 'border-rose-300 text-rose-800 hover:bg-rose-50',
    run: (t, n, a) => acceptTask(t, 'fail', n, a),
  },
  rerun: {
    label: '放回队列重跑',
    what: '定案 reworked 并搬回 inbox，下一轮跑批会重新认领。**会再花一次钱。**',
    hint: '例：已把 checks 里的路径写对，这次应该能过',
    tone: 'border-violet-300 text-violet-800 hover:bg-violet-50',
    run: (t, n) => rerunTask(t, n),
  },
}

export default function InterveneBar({
  taskId,
  attemptNo,
  onDone,
}: {
  taskId: string
  /** 验收落在哪一轮。省略 = 最后一轮。 */
  attemptNo?: number
  /** 成功后回调，让详情页重新拉数据。 */
  onDone: () => void
}) {
  const [open, setOpen] = useState<Action | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<ApiError | null>(null)
  const [ok, setOk] = useState<string | null>(null)

  const pick = (a: Action) => {
    // 切换动作时清掉上一次的理由和报错 —— 把「merged 的理由」留在
    // 「验收不通过」的输入框里，人很容易直接提交。
    setOpen((prev) => (prev === a ? null : a))
    setNote('')
    setErr(null)
    setOk(null)
  }

  const submit = async () => {
    if (!open || busy) return
    const spec = SPECS[open]
    setBusy(true)
    setErr(null)
    try {
      const res = await spec.run(taskId, note.trim(), attemptNo)
      setOk(res.message)
      setOpen(null)
      setNote('')
      onDone()
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError(0, { error: String(e) }))
      // 状态冲突（409）意味着服务端已经变了 —— 别人抢先处理了，或者上一次
      // 其实成功了。刷新让人看到真实状态，不然他会继续点。
      if (e instanceof ApiError && e.status === 409) onDone()
    } finally {
      setBusy(false)
    }
  }

  const tooShort = note.trim().length === 0
  const tooLong = note.trim().length > 512

  return (
    <div className="border-t border-slate-300 bg-slate-50">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          人工介入
        </span>
        {(Object.keys(SPECS) as Action[]).map((a) => (
          <button
            key={a}
            type="button"
            onClick={() => pick(a)}
            aria-expanded={open === a}
            className={`rounded border px-2 py-0.5 font-mono text-[11px] ${SPECS[a].tone} ${
              open === a ? 'bg-white ring-1 ring-slate-400' : 'bg-white'
            }`}
          >
            {SPECS[a].label}
          </button>
        ))}
      </div>

      {ok && (
        <div className="border-t border-emerald-200 bg-emerald-50 px-3 py-1.5 font-mono text-[11px] text-emerald-800">
          ✓ {ok}
        </div>
      )}

      {open && (
        <div className="border-t border-slate-200 bg-white px-3 py-2">
          <p className="mb-1.5 text-[11px] leading-relaxed text-slate-600">
            {SPECS[open].what}
          </p>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={SPECS[open].hint}
            rows={2}
            aria-label="介入理由"
            className="w-full rounded border border-slate-300 px-2 py-1 font-mono text-[11px] text-slate-800 placeholder:text-slate-400"
          />
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={submit}
              disabled={busy || tooShort || tooLong}
              className="rounded border border-slate-400 bg-slate-800 px-2.5 py-1 font-mono text-[11px] text-white disabled:opacity-40"
            >
              {busy ? '提交中…' : '确认'}
            </button>
            <button
              type="button"
              onClick={() => setOpen(null)}
              className="rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px] text-slate-600"
            >
              取消
            </button>
            <span
              className={`font-mono text-[10px] tabular-nums ${
                tooLong ? 'text-rose-600' : 'text-slate-400'
              }`}
            >
              {note.trim().length}/512
            </span>
            {tooShort && (
              <span className="font-mono text-[10px] text-slate-500">
                理由必填 —— 没理由的定案等于没发生
              </span>
            )}
          </div>
        </div>
      )}

      {err && (
        <div className="border-t border-rose-200 bg-rose-50 px-3 py-1.5 font-mono text-[11px]">
          <div className="text-rose-800">✗ {err.message}</div>
          {err.hint && <div className="mt-0.5 text-slate-600">→ {err.hint}</div>}
          {err.allowed && (
            <div className="mt-0.5 text-slate-500">可选值：{err.allowed.join(' / ')}</div>
          )}
        </div>
      )}
    </div>
  )
}
