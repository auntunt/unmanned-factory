/**
 * 控制室的状态 = 事件流的折叠。live 和 replay 发的是同一种事件
 * （见 factory/events.py 模块头），所以这里只有一个 reducer，不分模式。
 */

export type Bucket = 'inbox' | 'running' | 'done' | 'needs_human' | 'blocked'
export type Stage = 'dispatching' | 'judging' | 'merged' | 'escalated' | 'closed'
export type GateRole = 'regression' | 'scope' | 'spec' | 'architecture'
export const GATE_ROLES: GateRole[] = ['regression', 'scope', 'spec', 'architecture']

export interface Line {
  kind: 'tool' | 'text' | 'error'
  text: string
}

export interface AttemptState {
  key: string
  taskId: string
  no: number
  oracleClass: string
  classReason: string
  model: string
  stage: Stage
  costUsd: number
  tokensIn: number
  tokensOut: number
  wallMs: number
  hasDiff: boolean
  commit: string | null
  resolution: string
  note: string
  lines: Line[]
  verdicts: Partial<Record<string, { verdict: 'pass' | 'fail'; claims: number }>>
  /** performance.now() 时的开轮时刻，给屏幕上的计时器用；结果落地后停。 */
  openedAt: number
  /** 从事件里带来的真实时刻（回放里是几个月前）。 */
  at: string
}

export interface RoomState {
  tasks: Record<string, Bucket>
  /** 任务的一句话标题，来自 /api/tasks 的 prompt_preview。事件里没有。 */
  titles: Record<string, string>
  attempts: Record<string, AttemptState>
  /** 事件到达顺序，给「最新」用。 */
  order: string[]
  focus: string | null
  mergedCount: number
  totalCost: number
  lastEventAt: string
  replayDone: boolean
}

export const empty: RoomState = {
  tasks: {},
  titles: {},
  attempts: {},
  order: [],
  focus: null,
  mergedCount: 0,
  totalCost: 0,
  lastEventAt: '',
  replayDone: false,
}

export const keyOf = (taskId: string, no: number) => `${taskId}#${no}`

const MAX_LINES = 40

type Ev = Record<string, unknown> & { type: string; at?: string }

function stageOf(resolution: string, verdicts: object, hasResult: boolean): Stage {
  if (resolution === 'merged') return 'merged'
  if (resolution === 'escalated') return 'escalated'
  if (resolution && resolution !== 'pending') return 'closed'
  if (Object.keys(verdicts).length > 0 || hasResult) return 'judging'
  return 'dispatching'
}

function isLive(a: AttemptState) {
  return a.stage === 'dispatching' || a.stage === 'judging'
}

/** 焦点：最新还在跑的轮；都跑完了就取最新定案的那一轮。 */
function pickFocus(s: RoomState): string | null {
  for (let i = s.order.length - 1; i >= 0; i--) {
    const a = s.attempts[s.order[i]]
    if (a && isLive(a)) return a.key
  }
  return s.order.length ? s.order[s.order.length - 1] : null
}

function upsert(s: RoomState, taskId: string, no: number, patch: Partial<AttemptState>): AttemptState {
  const key = keyOf(taskId, no)
  const prev = s.attempts[key]
  const next: AttemptState = prev
    ? { ...prev, ...patch }
    : {
        key,
        taskId,
        no,
        oracleClass: '',
        classReason: '',
        model: '',
        stage: 'dispatching',
        costUsd: 0,
        tokensIn: 0,
        tokensOut: 0,
        wallMs: 0,
        hasDiff: false,
        commit: null,
        resolution: 'pending',
        note: '',
        lines: [],
        verdicts: {},
        openedAt: performance.now(),
        at: '',
        ...patch,
      }
  s.attempts[key] = next
  if (!prev) s.order.push(key)
  return next
}

export function reduce(state: RoomState, ev: Ev): RoomState {
  const s: RoomState = {
    ...state,
    tasks: { ...state.tasks },
    attempts: { ...state.attempts },
    order: [...state.order],
    lastEventAt: (ev.at as string) || state.lastEventAt,
  }
  const tid = ev.task_id as string
  const no = ev.attempt_no as number

  switch (ev.type) {
    case 'snapshot': {
      const buckets = (ev.tasks || {}) as Record<Bucket, string[]>
      s.tasks = {}
      for (const b of Object.keys(buckets) as Bucket[]) for (const id of buckets[b]) s.tasks[id] = b
      s.attempts = {}
      s.order = []
      s.mergedCount = 0
      s.totalCost = 0
      for (const raw of (ev.attempts || []) as Array<Record<string, unknown>>) {
        const verdicts: AttemptState['verdicts'] = {}
        for (const v of (raw.verdicts || []) as Array<{ role: string; verdict: 'pass' | 'fail'; claims: number }>)
          verdicts[v.role] = { verdict: v.verdict, claims: v.claims }
        const a = upsert(s, raw.task_id as string, raw.attempt_no as number, {
          oracleClass: raw.oracle_class as string,
          classReason: raw.class_reason as string,
          model: raw.model as string,
          costUsd: raw.cost_usd as number,
          tokensIn: raw.tokens_in as number,
          tokensOut: raw.tokens_out as number,
          wallMs: raw.wall_clock_ms as number,
          commit: (raw.commit as string) || null,
          resolution: raw.resolution as string,
          note: raw.resolution_note as string,
          stage: raw.stage as Stage,
          verdicts,
          at: raw.created_at as string,
        })
        s.totalCost += a.costUsd
        if (a.resolution === 'merged') s.mergedCount++
      }
      s.replayDone = false
      break
    }
    case 'task.state':
      s.tasks[tid] = ev.state as Bucket
      break
    case 'attempt.open':
      upsert(s, tid, no, {
        oracleClass: ev.oracle_class as string,
        classReason: ev.class_reason as string,
        model: ev.model as string,
        stage: 'dispatching',
        openedAt: performance.now(),
        at: ev.at as string,
      })
      break
    case 'attempt.line': {
      const a = upsert(s, tid, no, {})
      const lines = [...a.lines, { kind: ev.kind as Line['kind'], text: ev.text as string }]
      s.attempts[a.key] = { ...a, lines: lines.slice(-MAX_LINES) }
      break
    }
    case 'attempt.result': {
      const a = upsert(s, tid, no, {})
      const cost = ev.cost_usd as number
      s.totalCost += cost - a.costUsd
      s.attempts[a.key] = {
        ...a,
        costUsd: cost,
        tokensIn: ev.tokens_in as number,
        tokensOut: ev.tokens_out as number,
        wallMs: ev.wall_clock_ms as number,
        hasDiff: Boolean(ev.has_diff),
        stage: stageOf(a.resolution, a.verdicts, true),
      }
      break
    }
    case 'gate.verdict': {
      const a = upsert(s, tid, no, {})
      const verdicts = { ...a.verdicts, [ev.role as string]: { verdict: ev.verdict as 'pass' | 'fail', claims: ev.claims as number } }
      s.attempts[a.key] = { ...a, verdicts, stage: stageOf(a.resolution, verdicts, true) }
      break
    }
    case 'attempt.final': {
      const a = upsert(s, tid, no, {})
      const resolution = ev.resolution as string
      if (resolution === 'merged' && a.resolution !== 'merged') s.mergedCount++
      s.attempts[a.key] = {
        ...a,
        resolution,
        commit: (ev.commit as string) || null,
        note: (ev.note as string) || '',
        stage: stageOf(resolution, a.verdicts, true),
      }
      break
    }
    case 'replay.end':
      s.replayDone = true
      break
    default:
      return state
  }
  s.focus = pickFocus(s)
  return s
}

export const liveAttempts = (s: RoomState) => s.order.map((k) => s.attempts[k]).filter(isLive)
