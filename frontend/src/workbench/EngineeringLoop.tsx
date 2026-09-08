import { useId, useMemo } from 'react'
import { Link } from 'react-router-dom'

import type { EngineeringStage } from './v3-types'
import './engineering-loop.css'

interface Props {
  stages: EngineeringStage[]
  selectedId: string
  onSelect: (id: string) => void
}

const STAGE_ORDER = ['intake', 'plan', 'build', 'verify', 'deliver', 'reuse']
const ANGLES = [-90, -30, 30, 90, 150, 210]
const CENTER = 300
const ORBIT_RADIUS = 228

function point(angle: number, radius = ORBIT_RADIUS): { x: number; y: number } {
  const radians = angle * Math.PI / 180
  return { x: CENTER + Math.cos(radians) * radius, y: CENTER + Math.sin(radians) * radius }
}

function arcPath(index: number): string {
  const start = point(ANGLES[index] + 18)
  const end = point(ANGLES[index] + 30)
  return `M ${start.x.toFixed(2)} ${start.y.toFixed(2)} A ${ORBIT_RADIUS} ${ORBIT_RADIUS} 0 0 1 ${end.x.toFixed(2)} ${end.y.toFixed(2)}`
}

function shortTitle(value: string): string {
  const trimmed = value.trim()
  return trimmed.length > 42 ? `${trimmed.slice(0, 42)}…` : trimmed
}

export default function EngineeringLoop({ stages, selectedId, onSelect }: Props) {
  const markerId = `el-arrow-${useId().replace(/:/g, '')}`
  const orderedStages = useMemo(() => {
    const byId = new Map(stages.map((stage) => [stage.id, stage]))
    return STAGE_ORDER.map((id) => byId.get(id)).filter((stage): stage is EngineeringStage => Boolean(stage))
  }, [stages])
  const active = orderedStages.find((stage) => stage.id === selectedId) ?? orderedStages[0]
  const activeIndex = active ? orderedStages.findIndex((stage) => stage.id === active.id) : -1
  const firstItem = active?.items[0]

  return <div className="el-shell">
    <div className="el-loop" role="group" aria-label="软件工程闭环">
      <svg className="el-track" viewBox="0 0 600 600" aria-hidden="true" focusable="false">
        <defs><marker id={markerId} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
        <circle className="el-track-base" cx={CENTER} cy={CENTER} r={ORBIT_RADIUS} />
        {orderedStages.map((stage, index) => <path className={`el-track-segment ${index === orderedStages.length - 1 ? 'el-track-segment-final' : ''}`} d={arcPath(index)} markerEnd={`url(#${markerId})`} key={stage.id} />)}
      </svg>

      {orderedStages.map((stage, index) => <button
        className={`el-node ${active?.id === stage.id ? 'el-node-selected' : ''}`}
        key={stage.id}
        type="button"
        style={{ left: `${[50, 83, 83, 50, 17, 17][index] ?? 50}%`, top: `${[12, 31, 69, 88, 69, 31][index] ?? 50}%` }}
        aria-pressed={active?.id === stage.id}
        aria-controls="ov3-stage-details"
        onClick={() => onSelect(stage.id)}
      >
        <span className="el-node-number">0{index + 1}</span><span className="el-node-label">{stage.label}</span><strong>{stage.count}</strong><small>{stage.unit}</small>
      </button>)}

      {active && <div className="el-center" aria-live="polite">
        <span className="el-center-kicker">已选阶段</span><strong className="el-center-label">{active.label}</strong><b className="el-center-count">{active.count} <small>{active.unit}</small></b>
        {firstItem ? <Link className="el-center-item" to={firstItem.href}><span className="el-center-long">{shortTitle(firstItem.title)} <span aria-hidden="true">→</span></span><span className="el-center-short">查看记录 →</span></Link> : <span className="el-center-empty">这个阶段暂时没有记录</span>}
      </div>}
      {activeIndex < 0 && <div className="el-center" aria-live="polite"><span className="el-center-empty">暂无阶段数据</span></div>}
    </div>
    <p className="el-footnote">能力沉淀 → 验证并启用 → 回到下一次需求<br /><span>数量为阶段记录，不是完成率</span></p>
  </div>
}
