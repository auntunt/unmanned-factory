import Icon from './Icon'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Run } from '../workspace/types'
import { nextRunAction } from './run-guidance'

export default function RunActions({ run, canClarify, canDiscard, canCancel, busy, onDiscard, onCancel }: { run: Run; canClarify: boolean; canDiscard: boolean; canCancel: boolean; busy: boolean; onDiscard: () => void; onCancel: () => void }) {
  const [confirm, setConfirm] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const cancelTrigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDetailsElement>(null)
  const action = nextRunAction(run)
  useEffect(() => { if (confirm) { dialog.current?.showModal(); dialog.current?.querySelector<HTMLButtonElement>('button')?.focus() } }, [confirm])
  const close = () => { dialog.current?.close(); setConfirm(false); cancelTrigger.current?.focus() }
  return <div className="wb-detail-actions">
    <Link className="wb-button wb-button-primary" to={action.href}>{action.label} <Icon name="arrow" /></Link>
    <Link className="wb-button wb-button-secondary" to="/runs">返回看板</Link>
    <details className="wb-overflow" ref={menu} onKeyDown={e => { if (e.key === 'Escape' && !confirm) { e.currentTarget.open = false; e.currentTarget.querySelector('summary')?.focus() } }}><summary className="wb-button wb-button-secondary" aria-label="更多运行操作"><Icon name="triangle" className="wb-disclosure-icon" />···</summary><div>
      {canClarify && <Link to={`/runs/${encodeURIComponent(String(run.id))}?view=requirements`}>补充要求并生成下一版计划</Link>}
      <span>导出记录</span>{['json', 'markdown', 'zip'].map(format => <a key={format} href={`/api/v3/runs/${encodeURIComponent(String(run.id))}/export?format=${format}`}>{format.toUpperCase()}</a>)}
      {canDiscard && <button className="wb-button wb-button-secondary" disabled={busy} onClick={onDiscard}>标记为废弃</button>}
      {canCancel && <button ref={cancelTrigger} className="wb-button wb-button-danger" disabled={busy} onClick={() => setConfirm(true)}>{run.status === 'ready_for_review' ? '放弃本次交付' : '取消运行'}</button>}
    </div></details>
    {confirm && <dialog ref={dialog} className="wb-confirm" aria-labelledby="cancel-run-title" aria-describedby="cancel-run-description" onCancel={e => { e.preventDefault(); close() }}><h2 id="cancel-run-title">确认取消运行？</h2><p id="cancel-run-description">运行将停止，已产生的记录和证据会保留。</p><div className="wb-form-actions"><button className="wb-button wb-button-secondary" onClick={close}>保留运行</button><button className="wb-button wb-button-danger" disabled={busy} onClick={() => { close(); onCancel() }}>确认取消</button></div></dialog>}
  </div>
}
