import { useEffect, useMemo, useRef, useState } from 'react'
import './controlroom.css'
import {
  GATE_ROLES,
  liveAttempts,
  type AttemptState,
  type Bucket,
  type GateRole,
  type RoomState,
} from './events'
import { useEventStream, type Mode } from './useEventStream'

/**
 * 单屏控制室。视线从上往下走一遍就是一次完整的叙事：
 * 大数 → 工位 → 焦点任务 → 闸门 → 等人的那一条。
 *
 * 模式从地址栏来：`/?replay=T-142&speed=4` 回放，否则 live。
 * 前端不知道自己在放录像 —— 两条路进来的是同一种事件。
 */
export default function ControlRoom() {
  const mode = useMemo<Mode>(() => {
    const q = new URLSearchParams(window.location.search)
    const replay = q.get('replay')
    if (!replay) return { kind: 'live' }
    const speed = Number(q.get('speed') || '4')
    return { kind: 'replay', taskId: replay, speed: Number.isFinite(speed) ? speed : 4 }
  }, [])
  const { state, connected, problem } = useEventStream(mode)
  const [pinned, setPinned] = useState<string | null>(null)
  const focusKey = pinned && state.attempts[pinned] ? pinned : state.focus
  const focus = focusKey ? state.attempts[focusKey] : null
  const speed = mode.kind === 'replay' ? mode.speed : 1

  return (
    <div className="cr">
      <Head state={state} connected={connected} mode={mode} focus={focus} speed={speed} />
      <Workers state={state} focusKey={focusKey} onPick={setPinned} />
      <div className="cr-main">
        <Feed state={state} />
        <Focus state={state} a={focus} speed={speed} problem={problem} />
      </div>
      <Gates a={focus} />
      <Escalation state={state} />
    </div>
  )
}

// ---------- 顶栏 ----------

function Head({
  state, connected, mode, focus, speed,
}: { state: RoomState; connected: boolean; mode: Mode; focus: AttemptState | null; speed: number }) {
  const running = liveAttempts(state).length
  return (
    <header className="cr-head">
      <div className="name">
        <span className={'dot' + (connected ? '' : ' off')} />
        webuddy
        <span className="mode">
          {mode.kind === 'replay' ? `回放 ${mode.taskId} · ${mode.speed}×` : connected ? '实时' : '连接中'}
        </span>
      </div>
      <Stat k="运行中" v={String(running)} />
      <Stat k="已合并" v={String(state.mergedCount)} />
      <Stat k="累计花费" v={money(state.totalCost)} mono />
      <Elapsed a={focus} speed={speed} />
    </header>
  )
}

function Stat({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="cr-stat">
      <span className={'v' + (mono ? ' mono' : '')}>{v}</span>
      <span className="k">{k}</span>
    </div>
  )
}

/** 焦点那一轮的计时。跑完后停在墙钟上；跑着的时候每秒走。 */
function Elapsed({ a, speed }: { a: AttemptState | null; speed: number }) {
  const [, tick] = useState(0)
  const running = a && (a.stage === 'dispatching' || a.stage === 'judging') && a.wallMs === 0
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => tick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [running])
  let secs = 0
  if (a) secs = a.wallMs > 0 ? a.wallMs / 1000 : ((performance.now() - a.openedAt) / 1000) * speed
  return <Stat k={a ? `第 ${a.no} 轮用时` : '用时'} v={a ? clock(secs) : '—'} mono />
}

// ---------- 工位 ----------

const STAGE_LABEL: Record<AttemptState['stage'], string> = {
  dispatching: '派发中',
  judging: '闸门判收',
  merged: '已合并',
  escalated: '交给人',
  closed: '已定案',
}

function Workers({ state, focusKey, onPick }: { state: RoomState; focusKey: string | null; onPick: (k: string) => void }) {
  const live = liveAttempts(state).slice(-4)
  const slots: Array<AttemptState | null> = [...live]
  while (slots.length < 4) slots.push(null)
  return (
    <div className="cr-workers">
      {slots.map((a, i) =>
        a ? (
          <div key={a.key} className={'cr-worker' + (a.key === focusKey ? ' focus' : '')} onClick={() => onPick(a.key)}>
            <span className="id mono">工位 {i + 1} · {a.taskId}</span>
            <span className="stage">{STAGE_LABEL[a.stage]}{a.no > 1 ? ` · 第 ${a.no} 轮` : ''}</span>
          </div>
        ) : (
          <div key={`idle-${i}`} className="cr-worker idle">
            <span>工位 {i + 1} 空闲</span>
          </div>
        ),
      )}
    </div>
  )
}

// ---------- 工单流 ----------

const BUCKET_PILL: Record<Bucket, { cls: string; text: string }> = {
  inbox: { cls: 'ice', text: '入队' },
  running: { cls: 'pass', text: '运行' },
  done: { cls: 'dim', text: '已关' },
  needs_human: { cls: 'hold', text: '等人' },
  blocked: { cls: 'fail', text: '拦下' },
}
const BUCKET_ORDER: Bucket[] = ['running', 'inbox', 'needs_human', 'blocked', 'done']

function Feed({ state }: { state: RoomState }) {
  const rows = Object.entries(state.tasks)
    .sort((a, b) => BUCKET_ORDER.indexOf(a[1]) - BUCKET_ORDER.indexOf(b[1]) || b[0].localeCompare(a[0]))
    .slice(0, 12)
  return (
    <section className="cr-panel cr-feed">
      <h2>工单流</h2>
      {rows.length === 0 && <div className="row dim"><span /><span className="title">队列是空的</span></div>}
      {rows.map(([id, b]) => (
        <div key={id} className={'row' + (b === 'done' ? ' dim' : '')}>
          <span className={'pill ' + BUCKET_PILL[b].cls}>{BUCKET_PILL[b].text}</span>
          <span className="title"><span className="mono">{id}</span>{state.titles[id] ? ` ${state.titles[id]}` : ''}</span>
        </div>
      ))}
    </section>
  )
}

// ---------- 焦点任务 ----------

const STEPS = ['需求', '分级', '派发', '闸门', '合并', '关单'] as const

function stepStates(a: AttemptState, bucket: Bucket | undefined): Array<'done' | 'now' | 'stop' | ''> {
  const reached =
    a.stage === 'dispatching' ? 2 : a.stage === 'judging' ? 3 : a.stage === 'merged' ? (bucket === 'done' ? 5 : 4) : 3
  return STEPS.map((_, i) => {
    if (i < reached) return 'done'
    if (i === reached) return a.stage === 'escalated' || a.stage === 'closed' ? 'stop' : a.stage === 'merged' && reached === 5 ? 'done' : 'now'
    return ''
  })
}

function Focus({ state, a, speed, problem }: { state: RoomState; a: AttemptState | null; speed: number; problem: string | null }) {
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [a?.lines.length])

  if (!a) {
    return (
      <section className="cr-panel cr-focus">
        <div className="title"><h1>{problem || '等待第一个工单'}</h1></div>
        <div className="cr-log"><div className="empty">工单进来后这里会显示 agent 正在做什么。</div></div>
      </section>
    )
  }
  const active = a.stage === 'dispatching' || a.stage === 'judging'
  const title = state.titles[a.taskId] || a.taskId
  const cls = a.oracleClass ? `${a.oracleClass} 级` : ''
  const classPill = a.oracleClass === 'C' || a.oracleClass === 'D' ? 'hold' : 'ice'
  const bucket = state.tasks[a.taskId]
  void speed
  return (
    <section className="cr-panel cr-focus">
      <div className="title">
        <h1><span className="mono">{a.taskId}</span> {title}</h1>
        {cls && <span className={'pill ' + classPill}>{cls}{a.oracleClass === 'A' || a.oracleClass === 'B' ? ' · 无人' : ''}</span>}
        <span className="meta mono">第 {a.no} 轮 · {a.model || '—'}</span>
      </div>
      {a.classReason && <div className="reason">{a.classReason}</div>}
      <div className="cr-steps">
        {stepStates(a, bucket).map((s, i) => (
          <span key={STEPS[i]} className={'s ' + s}>{STEPS[i]}</span>
        ))}
      </div>
      <div className="cr-log mono" ref={logRef}>
        {a.lines.length === 0 && <div className="empty">{active ? '正在启动 agent' : '这一轮没有留下现场记录'}</div>}
        {a.lines.map((ln, i) => (
          <div key={i} className={'ln ' + ln.kind}>{ln.text}</div>
        ))}
        {active && a.wallMs === 0 && <div className="ln"><span className="cursor" /></div>}
      </div>
      <div className="foot mono">
        {a.wallMs > 0 && <span>{money(a.costUsd)} · {fmtK(a.tokensIn)} 进 {fmtK(a.tokensOut)} 出</span>}
        {a.hasDiff && <span className="plus">有改动</span>}
        {a.commit && <span className="commit">提交 {a.commit.slice(0, 7)}</span>}
        {problem && <span style={{ color: 'var(--hold)' }}>{problem}</span>}
        {a.stage === 'escalated' && <span style={{ color: 'var(--hold)' }}>{a.note || '三轮未过，已升级给人'}</span>}
      </div>
    </section>
  )
}

// ---------- 闸门 ----------

const GATE_LABEL: Record<GateRole, string> = {
  regression: '回归',
  scope: '范围',
  spec: '规格',
  architecture: '架构',
}

function Gates({ a }: { a: AttemptState | null }) {
  const judging = a?.stage === 'judging'
  let nextPending = true
  return (
    <div className="cr-gates">
      {GATE_ROLES.map((role) => {
        const v = a?.verdicts[role]
        let cls = ''
        let text = '等待'
        if (v) {
          cls = v.verdict
          text = v.verdict === 'pass' ? '通过' : v.claims ? `${v.claims} 处不过` : '不过'
        } else if (judging && nextPending) {
          cls = 'now'
          text = '核对中'
          nextPending = false
        }
        return (
          <div key={role} className={'cr-gate ' + cls}>
            <div className="role">{GATE_LABEL[role]}</div>
            <div className="verdict">{text}</div>
          </div>
        )
      })}
    </div>
  )
}

// ---------- 等人 ----------

function Escalation({ state }: { state: RoomState }) {
  const waiting = Object.entries(state.tasks).filter(([, b]) => b === 'needs_human' || b === 'blocked')
  if (waiting.length === 0) {
    return <div className="cr-bar"><span className="bell" style={{ opacity: 0.35 }} />没有等人处理的工单</div>
  }
  const [id, bucket] = waiting[waiting.length - 1]
  const last = [...state.order].reverse().map((k) => state.attempts[k]).find((x) => x.taskId === id)
  const why = bucket === 'blocked'
    ? '不可逆操作，永不无人执行'
    : last?.classReason || last?.note || '需要人看一眼'
  return (
    <div className="cr-bar hold">
      <span className="bell" />
      <span><span className="mono">{id}</span> · {why} · 已推送到手机等定案</span>
      <span className="when mono">{waiting.length > 1 ? `还有 ${waiting.length - 1} 条` : ''}</span>
    </div>
  )
}

// ---------- 格式 ----------

function money(n: number) {
  return `$${n.toFixed(2)}`
}

function clock(secs: number) {
  const s = Math.max(0, Math.floor(secs))
  const m = Math.floor(s / 60)
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}:${String(s % 60).padStart(2, '0')}`
}

function fmtK(n: number) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)
}
