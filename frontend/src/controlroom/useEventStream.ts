import { useEffect, useRef, useState } from 'react'
import { empty, reduce, type RoomState } from './events'

export type Mode = { kind: 'live' } | { kind: 'replay'; taskId: string; speed: number }

export interface Stream {
  state: RoomState
  connected: boolean
  /** 连接断了、还没重连上时的说明。屏幕上要显示，不然观众以为是死机。 */
  problem: string | null
}

const RECONNECT_MS = 2000

function urlFor(mode: Mode): string {
  if (mode.kind === 'live') return '/api/events'
  const q = new URLSearchParams({ replay: mode.taskId, speed: String(mode.speed) })
  return `/api/events?${q}`
}

/**
 * 一条 EventSource 喂一个 reducer。
 *
 * 为什么不把每个事件类型注册成单独的 listener：服务端 `event:` 字段和
 * `data.type` 是同一个值，用 onmessage 拿不到带 `event:` 的消息 —— 所以
 * 这里对每种类型都 addEventListener，统一交给 reduce。新增事件类型只需
 * 改 TYPES。
 */
const TYPES = [
  'snapshot', 'task.state', 'attempt.open', 'attempt.line',
  'attempt.result', 'gate.verdict', 'attempt.final', 'heartbeat', 'replay.end',
]

export function useEventStream(mode: Mode): Stream {
  const [state, setState] = useState<RoomState>(empty)
  const [connected, setConnected] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const stateRef = useRef(state)
  stateRef.current = state
  const modeKey = mode.kind === 'live' ? 'live' : `replay:${mode.taskId}:${mode.speed}`

  useEffect(() => {
    let es: EventSource | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let closed = false
    // 回放放完服务端会关流；那不是故障，不重连。
    let finished = false

    const open = () => {
      if (closed) return
      es = new EventSource(urlFor(mode))
      es.onopen = () => {
        setConnected(true)
        setProblem(null)
      }
      for (const t of TYPES) {
        es.addEventListener(t, (e) => {
          const ev = JSON.parse((e as MessageEvent).data)
          if (ev.type === 'replay.end') finished = true
          setState((prev) => reduce(prev, ev))
        })
      }
      es.onerror = () => {
        setConnected(false)
        es?.close()
        if (closed || finished) return
        setProblem('和工厂的连接断了，正在重连')
        timer = setTimeout(open, RECONNECT_MS)
      }
    }
    open()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      es?.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modeKey])

  // 标题（prompt 的第一句）不在事件里：任务列表接口拿一次，任务换桶时再拿。
  const taskKey = Object.keys(state.tasks).sort().join(',')
  useEffect(() => {
    let dead = false
    fetch('/api/tasks')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (dead || !data) return
        const titles: Record<string, string> = {}
        for (const rows of Object.values(data) as Array<Array<{ task_id: string; prompt_preview?: string }>>)
          for (const row of rows) titles[row.task_id] = row.prompt_preview || ''
        setState((prev) => ({ ...prev, titles: { ...prev.titles, ...titles } }))
      })
      .catch(() => undefined)
    return () => {
      dead = true
    }
  }, [taskKey])

  return { state, connected, problem }
}
