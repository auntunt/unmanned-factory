import type { Stage } from '../types'

// 闭环环形图：七个阶段节点绕成一圈，中心放总体闭环率。
//
// 形态借自一套 OA 监控台（8 节点环形流转图），但那套有个要命的问题：后端只有
// 4 个真实状态，前端画 8 个节点，其中 4 个是前端自己推的瞬时态、实测计数恒为
// 0。运维看见「复核中 0」得出的结论是「复核环节没跑」，而实际是任务在那个态
// 停留不到一秒。所以这里每个节点都带 source 标记，derived 的节点用虚线边框 +
// 角标，鼠标悬停给出推导依据。
//
// 纯 SVG，零图表库。七个节点的坐标是算出来的，不是摆出来的 —— 改 STAGE_NODES
// 的数量不用重排版面。

/** 节点色。终态绿、待人橙、阻塞红、其余按流转顺序给冷色。 */
const STAGE_COLOR: Record<string, string> = {
  inbox: '#94a3b8',
  running: '#0ea5e9',
  judging: '#6366f1',
  reworking: '#f59e0b',
  needs_human: '#f97316',
  blocked: '#ef4444',
  done: '#22c55e',
}

const SIZE = 460
const R_RING = 168
const R_NODE = 34

function polar(i: number, n: number, r: number): { x: number; y: number } {
  // 从正上方开始顺时针。-90° 是「12 点方向」。
  const a = ((i / n) * 360 - 90) * (Math.PI / 180)
  return {
    x: SIZE / 2 + r * Math.cos(a),
    y: SIZE / 2 + r * Math.sin(a),
  }
}

export default function ClosureRing({
  stages,
  closureRate,
  closureOf,
  closureHit,
}: {
  stages: Stage[]
  /** null = 算不出来（分母 0）。中心显示 — 而不是 0%。 */
  closureRate: number | null
  closureOf: number
  closureHit: number
}) {
  const n = stages.length
  if (n === 0) {
    return <p className="py-10 text-center text-sm text-slate-400">没有阶段定义</p>
  }

  const derivedCount = stages.filter((s) => s.source === 'derived').length

  return (
    <div className="flex flex-col items-center gap-3">
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="h-[420px] w-[420px] max-w-full"
        role="img"
        aria-label={`闭环流转图，${n} 个阶段，闭环率 ${
          closureRate === null ? '不可用' : `${(closureRate * 100).toFixed(1)}%`
        }`}
      >
        {/* 底环 */}
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={R_RING}
          fill="none"
          stroke="#e2e8f0"
          strokeWidth={2}
          strokeDasharray="4 4"
        />

        {/* 节点间的流向箭头。画在节点下面，所以先渲染。 */}
        {stages.map((s, i) => {
          const from = polar(i, n, R_RING)
          const to = polar((i + 1) % n, n, R_RING)
          // 从边缘起止而不是圆心，否则箭头会被节点圆盖住。
          const dx = to.x - from.x
          const dy = to.y - from.y
          const len = Math.hypot(dx, dy) || 1
          const pad = R_NODE + 6
          return (
            <line
              key={`arc-${s.key}`}
              x1={from.x + (dx / len) * pad}
              y1={from.y + (dy / len) * pad}
              x2={to.x - (dx / len) * pad}
              y2={to.y - (dy / len) * pad}
              stroke="#cbd5e1"
              strokeWidth={1.5}
              markerEnd="url(#ring-arrow)"
            />
          )
        })}

        <defs>
          <marker
            id="ring-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
          </marker>
        </defs>

        {/* 阶段节点 */}
        {stages.map((s, i) => {
          const { x, y } = polar(i, n, R_RING)
          const color = STAGE_COLOR[s.key] ?? '#94a3b8'
          const derived = s.source === 'derived'
          // count 为 null 是「队列读不到」，显示 — 且节点置灰；为 0 是真的空。
          const unknown = s.count === null
          return (
            <g key={s.key}>
              <title>
                {`${s.name}\n${s.desc}${
                  derived ? `\n\n派生态推导依据：${s.derived_from ?? '未说明'}` : ''
                }${unknown ? '\n\n当前读不到队列，这个数不可用' : ''}`}
              </title>
              <circle
                cx={x}
                cy={y}
                r={R_NODE}
                fill={unknown ? '#f8fafc' : '#ffffff'}
                stroke={unknown ? '#cbd5e1' : color}
                strokeWidth={2}
                strokeDasharray={derived ? '4 3' : undefined}
              />
              <text
                x={x}
                y={y - 2}
                textAnchor="middle"
                className="font-mono text-[17px] font-bold"
                fill={unknown ? '#94a3b8' : color}
              >
                {unknown ? '—' : s.count}
              </text>
              <text
                x={x}
                y={y + 13}
                textAnchor="middle"
                className="text-[9px]"
                fill="#64748b"
              >
                {s.name}
              </text>
              {derived && (
                <text
                  x={x + R_NODE - 6}
                  y={y - R_NODE + 10}
                  textAnchor="middle"
                  className="text-[9px] font-bold"
                  fill="#a855f7"
                >
                  ~
                </text>
              )}
            </g>
          )
        })}

        {/* 中心：闭环率 */}
        <circle cx={SIZE / 2} cy={SIZE / 2} r={78} fill="#f8fafc" stroke="#e2e8f0" />
        <text
          x={SIZE / 2}
          y={SIZE / 2 - 12}
          textAnchor="middle"
          className="font-mono text-[34px] font-bold"
          fill={closureRate === null ? '#94a3b8' : '#0f172a'}
        >
          {closureRate === null ? '—' : `${(closureRate * 100).toFixed(0)}%`}
        </text>
        <text
          x={SIZE / 2}
          y={SIZE / 2 + 10}
          textAnchor="middle"
          className="text-[10px]"
          fill="#64748b"
        >
          机器自闭环
        </text>
        <text
          x={SIZE / 2}
          y={SIZE / 2 + 28}
          textAnchor="middle"
          className="font-mono text-[10px]"
          fill="#94a3b8"
        >
          {closureHit}/{closureOf} 任务
        </text>
      </svg>

      {derivedCount > 0 && (
        <p className="max-w-lg text-center text-[11px] leading-relaxed text-slate-500">
          虚线圈（角标 <span className="font-bold text-purple-500">~</span>）的{' '}
          {derivedCount} 个节点是<strong>派生瞬时态</strong>，从 attempt
          字段推出来的，不是队列里数出来的。 计数是 0
          通常意味着任务在那个阶段停留极短，<strong>不代表那个环节没有工作</strong>。
        </p>
      )}
    </div>
  )
}
