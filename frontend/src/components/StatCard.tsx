// 大数字卡片。只负责把一个已经格式化好的值显示出来 ——
// 格式化（货币、小数位、null → —）留在调用方，因为「这个数拿不到」和
// 「这个数是 0」的区别只有调用方知道语义，卡片不该自己猜。

export interface StatCardProps {
  /** 卡片标签，如「总任务数」 */
  label: string
  /** 已格式化的主数值。传 null 表示这个数拿不到，渲染成 — */
  value: string | null
  /** 数值下方的一行小字说明（可选），用来解释这个数是怎么来的 */
  hint?: string
  /** true 时主数值用醒目的 emerald 色（花费类指标） */
  accent?: boolean
}

export default function StatCard({
  label,
  value,
  hint,
  accent = false,
}: StatCardProps) {
  const unavailable = value === null

  // 拿不到的数用灰色 + —，不要落到 0：0 是一个断言，— 是「不知道」。
  const valueClass = unavailable
    ? 'text-4xl font-bold tabular-nums text-slate-300'
    : accent
      ? 'text-4xl font-bold tabular-nums text-emerald-600'
      : 'text-4xl font-bold tabular-nums text-slate-900'

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="text-sm font-medium text-slate-500">{label}</div>
      <div className={`mt-2 ${valueClass}`}>
        {unavailable ? '—' : value}
      </div>
      {hint ? (
        <div className="mt-1 text-xs leading-relaxed text-slate-400">{hint}</div>
      ) : null}
    </div>
  )
}
