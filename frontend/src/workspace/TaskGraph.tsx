import { useMemo, useState } from 'react'
import type { PlanTask, TaskStatus } from './types'

interface Props {
  tasks: PlanTask[]
}

interface Position {
  task: PlanTask
  x: number
  y: number
  level: number
}

const taskStatusLabels: Record<TaskStatus, string> = {
  pending: '待执行', queued: '已排队', running: '执行中', verified: '已验证', completed: '已完成', failed: '失败', blocked: '已阻塞', cancelled: '已取消',
}

function taskLevels(tasks: PlanTask[]): Position[] {
  const byId = new Map(tasks.map((task) => [task.id, task]))
  const memo = new Map<string, number>()
  const visiting = new Set<string>()
  const levelOf = (id: string): number => {
    if (memo.has(id)) return memo.get(id) ?? 0
    if (visiting.has(id)) return 0
    visiting.add(id)
    const task = byId.get(id)
    const level = task ? Math.max(0, ...task.depends_on.filter((dep) => byId.has(dep)).map(levelOf).map((value) => value + 1)) : 0
    visiting.delete(id)
    memo.set(id, level)
    return level
  }
  const levels = new Map<number, PlanTask[]>()
  tasks.forEach((task) => {
    const level = levelOf(task.id)
    const group = levels.get(level) ?? []
    group.push(task)
    levels.set(level, group)
  })
  const positions: Position[] = []
  Array.from(levels.entries()).sort(([a], [b]) => a - b).forEach(([level, group]) => {
    group.forEach((task, index) => positions.push({ task, level, x: 24 + level * 218, y: 28 + index * 112 }))
  })
  return positions
}

export default function TaskGraph({ tasks }: Props) {
  const [selected, setSelected] = useState<string | null>(tasks[0]?.id ?? null)
  const positions = useMemo(() => taskLevels(tasks), [tasks])
  const byId = new Map(positions.map((position) => [position.task.id, position]))
  const width = Math.max(650, (Math.max(0, ...positions.map((position) => position.level)) + 1) * 218)
  const height = Math.max(190, Math.max(0, ...positions.map((position) => position.y)) + 104)
  const selectedTask = tasks.find((task) => task.id === selected)

  if (!tasks.length) return <div className="wf-empty wf-graph-empty">当前计划没有可视化任务。</div>

  return (
    <div className="wf-graph-wrap">
      <div className="wf-graph-scroll">
        <svg className="wf-graph" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="任务依赖图">
          <defs>
            <marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#a5b1c4" />
            </marker>
          </defs>
          {positions.flatMap((position) => position.task.depends_on.map((dependency) => {
            const parent = byId.get(dependency)
            if (!parent) return null
            return <line key={`${dependency}-${position.task.id}`} x1={parent.x + 178} y1={parent.y + 42} x2={position.x - 8} y2={position.y + 42} stroke="#a5b1c4" strokeWidth="1.5" markerEnd="url(#wf-arrow)" />
          }))}
          {positions.map((position) => {
            const isSelected = position.task.id === selected
            return (
              <g key={position.task.id} className={`wf-node ${isSelected ? 'is-selected' : ''}`} onClick={() => setSelected(position.task.id)} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') setSelected(position.task.id) }}>
                <rect x={position.x} y={position.y} width="178" height="84" rx="10" fill={isSelected ? '#eaf1fb' : '#fff'} stroke={isSelected ? '#315f9b' : '#d8e0eb'} strokeWidth={isSelected ? 2 : 1} />
                <text x={position.x + 14} y={position.y + 23} className="wf-node-id">{position.task.id}</text>
                <text x={position.x + 14} y={position.y + 46} className="wf-node-title">{position.task.title.slice(0, 22)}{position.task.title.length > 22 ? '…' : ''}</text>
                <text x={position.x + 14} y={position.y + 67} className={`wf-node-meta wf-task-status-${position.task.status ?? 'pending'}`}>{taskStatusLabels[position.task.status ?? 'pending']} · {position.task.complexity} · {position.task.risk} 风险</text>
              </g>
            )
          })}
        </svg>
      </div>
      {selectedTask && (
        <aside className="wf-criterion" aria-label="任务范围和验收标准">
          <div className="wf-criterion-kicker">{selectedTask.id} · 任务详情</div>
          <h4>{selectedTask.title}</h4>
          <p>{selectedTask.prompt}</p>
          <div className="wf-detail-block"><strong>范围</strong>{selectedTask.paths.length ? <ul>{selectedTask.paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul> : <span className="wf-muted">未提供路径</span>}</div>
          <div className="wf-detail-block"><strong>验收标准</strong>{selectedTask.acceptance.length ? <ul>{selectedTask.acceptance.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul> : <span className="wf-muted">未提供验收标准</span>}</div>
          <div className="wf-detail-block"><strong>依赖</strong><span>{selectedTask.depends_on.length ? selectedTask.depends_on.join('、') : '无'}</span></div>
        </aside>
      )}
    </div>
  )
}
