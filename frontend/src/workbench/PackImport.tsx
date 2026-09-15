import { useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Icon from './Icon'
import NativePackImport from './NativePackImport'
import SkillIngestion from './SkillIngestion'
import type { PageProps } from './ui'

const tabs = ['职能包 v1/v2', '外部 skill 包·适配人签']
export default function PackImport(props: PageProps & { onImported: (id: string) => void }) {
  const [params] = useSearchParams()
  const hasReview = Boolean(params.get('project'))
  const [tab, setTab] = useState(hasReview ? 1 : 0)
  const buttons = useRef<(HTMLButtonElement | null)[]>([])
  return <details className="wb-card wb-pack-import" open={hasReview || undefined}>
    <summary><Icon name="triangle" className="wb-disclosure-icon" />导入</summary>
    <div className="wb-import-tabs" role="tablist" aria-label="导入类型">{tabs.map((label, i) => <button key={label} ref={el => { buttons.current[i] = el }} type="button" className="wb-button wb-button-secondary" role="tab" id={`pack-tab-${i}`} aria-selected={tab === i} aria-controls={`pack-panel-${i}`} tabIndex={tab === i ? 0 : -1} onClick={() => setTab(i)} onKeyDown={e => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
      e.preventDefault(); const next = e.key === 'Home' ? 0 : e.key === 'End' ? 1 : 1 - i
      setTab(next); buttons.current[next]?.focus()
    }}>{label}</button>)}</div>
    <div className="wb-import-panel" role="tabpanel" id="pack-panel-0" aria-labelledby="pack-tab-0" hidden={tab !== 0}><NativePackImport {...props} /></div>
    <div className="wb-import-panel" role="tabpanel" id="pack-panel-1" aria-labelledby="pack-tab-1" hidden={tab !== 1}><SkillIngestion {...props} /></div>
  </details>
}
