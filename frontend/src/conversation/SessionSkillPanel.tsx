import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { request } from '../workspace/api'
import { errorText, type PageProps } from '../workbench/ui'
import Icon from '../workbench/Icon'

// ---------- local types (N5 owns these; no import from workspace/types.ts) ----------

/** A single session-scoped skill binding, as returned by
 *  GET /api/v4/sessions/{sid}/skills → items[]. */
export interface SessionSkill {
  id: string
  session_id: string
  name: string
  origin: 'zip' | 'github'
  source_ref: string
  source_version: string
  source_sha256: string
  entry: string
  description: string
  dependencies: string[]
  import_state: 'imported' | 'rejected'
  dependency_state: 'ready' | 'missing' | 'unknown'
  actor_id: string
  created_at: string
  /** Present only if the skill has been executed in this session */
  last_execution_success?: boolean | null
  /** Human-readable reason when import_state is 'rejected' */
  reject_reason?: string
}

// ---------- import-state badges ----------

function ImportBadge({ state, reason }: { state: SessionSkill['import_state']; reason?: string }) {
  if (state === 'rejected') {
    return (
      <span className="ss-badge ss-badge-rejected" title={reason}>
        导入失败{reason ? `：${reason}` : ''}
      </span>
    )
  }
  return <span className="ss-badge ss-badge-imported">已导入</span>
}

function DependencyBadge({ state, deps }: { state: SessionSkill['dependency_state']; deps: string[] }) {
  if (state === 'missing') {
    return (
      <span className="ss-badge ss-badge-missing">
        需要管理员补齐{deps.length > 0 ? `：${deps.join('、')}` : '运行条件'}
      </span>
    )
  }
  if (state === 'unknown') {
    return <span className="ss-badge ss-badge-unknown">依赖状态未知</span>
  }
  return <span className="ss-badge ss-badge-ready">依赖就绪</span>
}

function ExecutionBadge({ success }: { success?: boolean | null }) {
  if (success === true) return <span className="ss-badge ss-badge-exec-ok">最近执行成功</span>
  if (success === false) return <span className="ss-badge ss-badge-exec-fail">最近执行失败</span>
  return <span className="ss-badge ss-badge-exec-none">尚未执行</span>
}

// ---------- main panel ----------

/** Session-scoped skill panel: list / import / remove skills bound to ONE session.
 *
 *  Renders three independent status lines per skill:
 *    1. import_state  (imported / rejected)
 *    2. dependency_state  (ready / missing → "需要管理员补齐" / unknown)
 *    3. last execution result (from run history, if available)
 *
 *  Mounted inside RunWorkspace; sessionId = agent_conversations.id. */
export default function SessionSkillPanel({
  sessionId,
  csrfToken,
  onUnauthorized,
}: PageProps & { sessionId: string }) {
  const [skills, setSkills] = useState<SessionSkill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const gen = useRef(0)

  const base = `/api/v4/sessions/${encodeURIComponent(sessionId)}/skills`

  const load = useCallback(async () => {
    const mine = ++gen.current
    try {
      const data = await request<{ items: SessionSkill[] }>(base, { onUnauthorized })
      if (gen.current === mine) {
        setSkills(data.items)
        setError(null)
      }
    } catch (cause) {
      if (gen.current === mine) setError(errorText(cause))
    } finally {
      if (gen.current === mine) setLoading(false)
    }
  }, [base, onUnauthorized])

  useEffect(() => {
    setLoading(true)
    void load()
    return () => { gen.current += 1 }
  }, [load])

  const importSkill = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || busy) return
    setBusy(true)
    setError(null)
    try {
      const body = new FormData()
      body.append('file', file)
      await request(base, { method: 'POST', csrfToken, onUnauthorized, body })
      await load()
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (skillId: string) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await request(`${base}/${encodeURIComponent(skillId)}`, {
        method: 'DELETE',
        csrfToken,
        onUnauthorized,
      })
      setSkills(prev => prev.filter(s => s.id !== skillId))
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="ss-panel" aria-label="会话 Skill">
      <details>
        <summary>
          <Icon name="triangle" className="cv-disclosure" width={13} height={13} />
          当前会话加载的 Skill · {skills.length} 项
        </summary>

        <div className="ss-body">
          <p className="ss-hint">
            加载到本会话的方法只在本次会话有效，不影响团队能力库。
          </p>

          <label className="ss-import" title="上传 Skill ZIP 加载到当前会话">
            <Icon name="plus" width={14} height={14} />
            {busy ? '正在加载…' : '加载 Skill'}
            <input
              type="file"
              accept=".zip"
              disabled={busy}
              onChange={event => void importSkill(event)}
            />
          </label>

          {error && (
            <div className="ss-error" role="alert">
              {error}
            </div>
          )}

          {loading && <p role="status">正在读取会话 Skill…</p>}

          {!loading && skills.length === 0 && (
            <p className="ss-empty">本会话尚未加载任何 Skill。</p>
          )}

          {skills.map(skill => (
            <article className="ss-card" key={skill.id} data-testid={`ss-card-${skill.id}`}>
              <div className="ss-card-head">
                <strong>{skill.name}</strong>
                <small>
                  来源：{skill.origin === 'github' ? skill.source_ref : 'ZIP'} ·
                  版本：{skill.source_version}
                </small>
              </div>

              {skill.description && (
                <p className="ss-desc">{skill.description}</p>
              )}

              <div className="ss-status" aria-label="状态">
                <ImportBadge state={skill.import_state} reason={skill.reject_reason} />
                <DependencyBadge state={skill.dependency_state} deps={skill.dependencies} />
                <ExecutionBadge success={skill.last_execution_success} />
              </div>

              <button
                className="ss-remove"
                type="button"
                disabled={busy}
                onClick={() => void remove(skill.id)}
                aria-label={`移除 ${skill.name}`}
              >
                移除
              </button>
            </article>
          ))}
        </div>
      </details>
    </section>
  )
}
