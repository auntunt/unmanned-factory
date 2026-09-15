import Icon from './Icon'
import { useLayoutEffect, useRef, useState } from 'react'
import type { AuditEvent } from '../workspace/types'
import { formatDate } from './ui'
export function logEntry(event: AuditEvent) {
  const payload = event.payload && typeof event.payload === 'object' ? event.payload as Record<string,unknown> : {}
  const kind = /error|fail/i.test(event.type) || payload.error || payload.stderr ? 'error' : /command|tool/i.test(event.type) || payload.command ? 'command' : /model|assistant|output/i.test(event.type) || payload.model || payload.output ? 'model' : 'event'
  const text = typeof event.payload === 'string' ? event.payload : ['command','stdout','stderr','output','text','message','error','summary','reason','model'].map(key => payload[key]).filter(value => typeof value === 'string').join('\n') || event.type
  return { kind, label: { error: '错误', command: '命令', model: '模型输出', event: '运行事件' }[kind], text }
}
export default function ExecutionLog({ events, error }: { events: AuditEvent[] | null; error?: string | null }) {
  const viewport = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const previousTop = useRef(0)
  const [paused, setPaused] = useState(false)
  const bottom = () => { const node = viewport.current; if (node) { node.scrollTop = node.scrollHeight; previousTop.current = node.scrollTop } }
  useLayoutEffect(() => { if (follow.current) bottom() }, [events])
  return <section className="wb-execution-card"><div className="wb-log-heading"><h2>执行日志</h2><small>{paused ? '已暂停自动滚动' : '跟随最新记录'}</small></div>{error && <p role="alert">读取日志失败：{error}，已保留现有记录。</p>}<div className="wb-log-wrap"><div ref={viewport} className="wb-log-stream" role="log" aria-label="执行日志流" aria-live={paused ? 'off' : 'polite'} aria-relevant="additions" tabIndex={0} onWheel={event => { if (event.deltaY < 0) { follow.current = false; setPaused(true) } }} onScroll={event => {
    const node = event.currentTarget
    const atBottom = node.scrollHeight - node.clientHeight - node.scrollTop <= 8
    if (node.scrollTop < previousTop.current || !atBottom) { follow.current = false; setPaused(true) }
    else if (atBottom) { follow.current = true; setPaused(false) }
    previousTop.current = node.scrollTop
  }}>
  {!events?.length && <p>{events === null ? '正在读取运行日志…' : '尚无已记录事件，等待下次同步。'}</p>}
  {events?.map(event => { const entry = logEntry(event); const at = new Date(event.at); const stamp = Number.isFinite(at.getTime()) ? at.toLocaleTimeString('zh-CN', { hour12: false }) : '时间未知'; return entry.text.split('\n').map((line, index) => <div key={`${event.id}:${index}`} className={`wb-log-line wb-log-${entry.kind}`}><time title={formatDate(event.at)} dateTime={event.at}>{stamp}</time><span>{entry.label}</span><span>{line || '\u00a0'}</span></div>) })}
  </div>{paused && <button className="wb-log-jump" onClick={() => { follow.current = true; setPaused(false); bottom() }}>跳到最新 <Icon name="down" /></button>}</div></section>
}
