import { Link } from 'react-router-dom'
import { PROJECT_STAGES, projectStageHref } from './project-stages'
import type { EngineeringStage } from './v3-types'
import type { Run } from '../workspace/types'
import { relativeTime, runTitle, StatusDot, tabKeys } from './presentation'

export default function StageStrip({ stages, projectId, selectedId, onSelect, runs = [] }: { stages: EngineeringStage[]; projectId: string | number; selectedId?: string; onSelect?: (id: string) => void; runs?: Run[] }) {
  return <div><nav className="wb-stage-strip" aria-label="工程阶段" role={onSelect ? 'tablist' : undefined} onKeyDown={onSelect ? tabKeys : undefined}>
    {PROJECT_STAGES.map(stage => {
      const data = stages.find(s => s.id === stage.id)
      const latest = [...(data?.items || [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
      const run = latest && runs.find(r => String(r.id) === latest.id)
      const title = run ? runTitle(run) : latest?.title
      const content = <><strong>{stage.label}</strong><span className="wb-stage-latest" title={title}>{title ? Array.from(title).slice(0, 14).join('') + (Array.from(title).length > 14 ? '…' : '') : '暂无记录'}</span><StatusDot status={latest?.status} /><small>{data?.count ?? 0} {data?.unit ?? '条记录'}{latest && <> · <time dateTime={latest.updated_at}>{relativeTime(latest.updated_at)}</time></>}</small></>
      return onSelect ? <button type="button" role="tab" id={`stage-${stage.id}`} aria-controls="ov3-stage-details" aria-selected={selectedId === stage.id} tabIndex={selectedId === stage.id ? 0 : -1} key={stage.id} onClick={() => onSelect(stage.id)}>{content}</button> : <Link key={stage.id} to={projectStageHref(projectId, stage.id)} aria-current={selectedId === stage.id ? 'page' : undefined}>{content}</Link>
    })}
  </nav><p className="wb-stage-note">六阶段按实际记录分组，数量不代表完成率。</p></div>
}
