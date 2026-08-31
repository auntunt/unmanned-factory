import { RING } from './tokens'

/** 环形布局：起始角 -90°（正上方），顺时针等分 */
export function nodePositions(count: number) {
  const step = 360 / count
  return Array.from({ length: count }, (_, i) => {
    const deg = RING.startAngle + i * step
    const rad = (deg * Math.PI) / 180
    return {
      index: i,
      deg,
      x: RING.size / 2 + RING.radius * Math.cos(rad),
      y: RING.size / 2 + RING.radius * Math.sin(rad),
    }
  })
}

/** 主流程连线：沿圆弧走，两端留出节点半径的间隙 */
export function arcBetween(
  a: { x: number; y: number },
  b: { x: number; y: number },
  gap = RING.nodeSize / 2 + 6,
) {
  const cx = RING.size / 2
  const cy = RING.size / 2
  const shrink = (p: { x: number; y: number }, toward: { x: number; y: number }) => {
    const dx = toward.x - p.x
    const dy = toward.y - p.y
    const len = Math.hypot(dx, dy) || 1
    return { x: p.x + (dx / len) * gap, y: p.y + (dy / len) * gap }
  }
  const s = shrink(a, b)
  const e = shrink(b, a)
  const r = Math.hypot(s.x - cx, s.y - cy)
  return `M ${s.x} ${s.y} A ${r} ${r} 0 0 1 ${e.x} ${e.y}`
}

/** 旁路连线：直线穿过圆内，虚线画 */
export function chordBetween(
  a: { x: number; y: number },
  b: { x: number; y: number },
  gap = RING.nodeSize / 2 + 6,
) {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len
  const uy = dy / len
  return `M ${a.x + ux * gap} ${a.y + uy * gap} L ${b.x - ux * gap} ${b.y - uy * gap}`
}

export function fmtHours(ms: number) {
  const h = ms / 3600000
  return h >= 24 ? `${(h / 24).toFixed(1)} 天` : `${h.toFixed(1)} 小时`
}

export function fmtDelta(d: number | null | undefined) {
  if (d == null) return null
  const pct = (d * 100).toFixed(1)
  return { text: `${d >= 0 ? '+' : ''}${pct}%`, up: d >= 0 }
}

export function fmtUsd(v: number | null | undefined) {
  if (v == null) return '—'
  return v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(3)}`
}

/**
 * 比率的诚实渲染：分母为 0 时给 null，不给 0。
 *
 * 复刻件要点 #4 的工厂版：验收覆盖率 0/26 时显示 0% 会被读成
 * 「验过 26 单全不合格」，实际是「26 单一次没验」。两回事。
 */
export function safeRate(num: number, den: number): number | null {
  return den > 0 ? num / den : null
}

export function fmtRate(r: number | null | undefined, digits = 1) {
  return r == null ? '—' : `${(r * 100).toFixed(digits)}%`
}

export function ago(iso: string) {
  const ms = Date.now() - new Date(iso).getTime()
  const h = ms / 3600000
  if (h < 1) return `${Math.max(1, Math.round(ms / 60000))} 分钟前`
  if (h < 24) return `${Math.round(h)} 小时前`
  return `${Math.floor(h / 24)} 天前`
}
