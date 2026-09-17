import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { request } from '../workspace/api'
import { errorText, formatDate, type PageProps } from './ui'
import {
  ENV_LABEL, LIFECYCLE_LABEL, SUPPORT_LABEL, packsBase,
  type Evaluation, type PackDetail as Detail, type SupportRow,
} from './pack-types'
import './packs.css'

const TABS = [['overview', '概览'], ['content', '内容'], ['validation', '验证'], ['versions', '版本']] as const
type Tab = typeof TABS[number][0]

function Support({ rows }: { rows: SupportRow[] }) {
  return <ul className="pk-list">{rows.map(row => <li key={row.format} className="pk-row">
    <span>{row.format}{row.evidence && <><br /><code>{row.evidence}</code></>}</span>
    <span className={`pk-pill ${row.status === 'supported' ? 'is-published' : row.status === 'partial' ? 'is-warn' : 'is-error'}`}>
      {SUPPORT_LABEL[row.status]}</span>
  </li>)}</ul>
}

function EvidenceCard({ evaluation }: { evaluation: Evaluation }) {
  return <div className="pk-section">
    <h2>{evaluation.passed ? '验证通过' : '验证未通过'} · {formatDate(evaluation.created_at)}</h2>
    <p className="pk-muted">{evaluation.summary}</p>
    <dl className="pk-kv">
      <dt>候选内容摘要</dt><dd><code>{evaluation.content_digest.slice(0, 16)}…</code></dd>
      <dt>测试集</dt><dd>{evaluation.test_set || '—'}</dd>
      <dt>执行环境</dt><dd>{evaluation.environment?.python ? `Python ${evaluation.environment.python}` : '—'} ·
        隔离 {evaluation.environment?.sandbox === 'seatbelt' ? '已启用' : '本机不可用（未隔离）'}</dd>
    </dl>
    <ul className="pk-list">{evaluation.cases.map(item => <li key={item.id} className="pk-row">
      <span>{item.id}{item.reason && <><br /><code>{item.reason}</code></>}</span>
      <span className={`pk-pill ${item.passed ? 'is-published' : 'is-error'}`}>{item.passed ? '通过' : '未通过'}</span>
    </li>)}</ul>
  </div>
}

/** 职能包详情：概览 / 内容 / 验证 / 版本。主操作随状态变化——继续修复、运行验证、
 *  发布版本或直接使用。发布条件由服务端复核，这里只呈现服务端给出的状态与原因。 */
export default function PackDetail({ csrfToken, onUnauthorized }: PageProps) {
  const { packId } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = (TABS.find(([key]) => key === params.get('tab'))?.[0] ?? 'overview') as Tab
  const id = packId ? encodeURIComponent(packId) : ''
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'' | 'validate' | 'publish'>('')
  const [notice, setNotice] = useState<string | null>(null)
  const gen = useRef(0)

  const load = useCallback(async () => {
    const mine = (gen.current += 1)
    try {
      const value = await request<Detail>(`${packsBase}/${id}`, { onUnauthorized })
      if (gen.current === mine) { setDetail(value); setError(null) }
    } catch (cause) { if (gen.current === mine) setError(errorText(cause)) }
  }, [id, onUnauthorized])
  useEffect(() => { void load() }, [load])

  const draft = detail?.draft ?? null
  const currentEvidence = draft?.evaluations?.find(item => item.content_digest === draft.content_digest) ?? null

  const validate = async () => {
    if (!draft || busy) return
    setBusy('validate'); setError(null); setNotice('正在用包内测试集真实执行验证…')
    try {
      const started = await request<{ job_id: string }>(`${packsBase}/${id}/evaluations`, {
        method: 'POST', csrfToken, onUnauthorized,
        body: { expected_revision: draft.revision, operation_key: crypto.randomUUID() },
      })
      const deadline = Date.now() + 180_000
      for (;;) {
        const job = await request<{ status: string; result?: { passed?: boolean; summary?: string }; error?: string }>(
          `${packsBase}/jobs/${encodeURIComponent(started.job_id)}`, { onUnauthorized })
        if (['completed', 'failed', 'cancelled', 'interrupted'].includes(job.status)) {
          setNotice(job.result?.summary || job.error || `验证作业状态：${job.status}`)
          break
        }
        if (Date.now() > deadline) { setNotice('验证仍在进行，可稍后回到本页查看结果。'); break }
        await new Promise(resolve => setTimeout(resolve, 1200))
      }
      await load()
    } catch (cause) { setError(errorText(cause)); setNotice(null) } finally { setBusy('') }
  }

  const publish = async () => {
    if (!draft || !currentEvidence || busy) return
    setBusy('publish'); setError(null)
    try {
      const version = await request<{ version: number }>(`${packsBase}/${id}/versions`, {
        method: 'POST', csrfToken, onUnauthorized,
        body: { expected_revision: draft.revision, evaluation_id: currentEvidence.id, operation_key: crypto.randomUUID() },
      })
      setNotice(`已发布 v${version.version}。这是工作区内部的版本登记，不等于生产部署。`)
      await load()
    } catch (cause) { setError(errorText(cause)) } finally { setBusy('') }
  }

  if (error && !detail) return <div className="pk-notice pk-notice-error" role="alert"><strong>无法打开职能包</strong><p>{error}</p></div>
  if (!detail) return <p className="pk-muted">正在载入职能包…</p>

  const published = detail.versions[0] ?? null
  const environment = published ? detail.environments[published.id] : undefined
  const lifecycle = draft?.lifecycle ?? (published ? 'published' : 'draft')

  return <div className="wb-page pk-detail">
    <div className="pk-detail-head">
      <h1>{detail.name}</h1>
      <span className={`pk-pill ${lifecycle === 'published' ? 'is-published' : lifecycle === 'needs_changes' ? 'is-error' : 'is-draft'}`}>
        {LIFECYCLE_LABEL[lifecycle]}</span>
      {published && <span className={`pk-pill ${environment?.status === 'ready' ? 'is-published' : environment?.status === 'unavailable' ? 'is-warn' : 'is-draft'}`}>
        {ENV_LABEL[environment?.status ?? 'unchecked']}</span>}
      <span style={{ marginLeft: 'auto' }} />
      <Link className="pk-button pk-button-secondary" to="/ability-center?tab=packs">返回能力库</Link>
    </div>
    <p className="pk-muted">{detail.purpose}</p>

    <nav className="pk-tabs" role="tablist" aria-label="职能包详情">
      {TABS.map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} id={`pk-tab-${key}`}
        aria-controls="pk-panel" tabIndex={tab === key ? 0 : -1}
        onClick={() => setParams(next => { const value = new URLSearchParams(next); value.set('tab', key); return value }, { replace: true })}>{label}</button>)}
    </nav>

    {notice && <div className="pk-notice pk-notice-info">{notice}</div>}
    {error && <div className="pk-notice pk-notice-error" role="alert">{error}</div>}
    {draft?.blocked_reason && <div className="pk-notice pk-notice-warn"><strong>还不能发布</strong><p>{draft.blocked_reason}</p></div>}

    <section className="pk-panel" role="tabpanel" id="pk-panel" aria-labelledby={`pk-tab-${tab}`}>
      {tab === 'overview' && <>
        <div className="pk-section">
          <h2>这个能力做什么</h2>
          <p className="pk-muted">{detail.purpose || '尚未填写用途'}</p>
          <dl className="pk-kv">
            <dt>调用入口</dt><dd>{draft?.manifest?.tool.entrypoint || published?.manifest.tool.entrypoint || '—'}</dd>
            <dt>依赖锁</dt><dd>{(draft?.manifest ?? published?.manifest)?.dependency_lock.packages.join('、') || '仅标准库'}</dd>
            <dt>来源任务</dt><dd>{draft?.source_task_id
              ? <Link className="pk-link" to={`/runs/${encodeURIComponent(draft.source_task_id)}`}>{draft.source_task_id.slice(0, 8)}</Link>
              : '—'}</dd>
          </dl>
        </div>
        {(draft?.manifest ?? published?.manifest) && <div className="pk-section">
          <h2>适用范围</h2>
          <p className="pk-muted">只有写明「已验证」的格式有真实证据；其余按标注对待，不代表通用。</p>
          <Support rows={(draft?.manifest ?? published!.manifest).support_matrix} />
        </div>}
        {published && environment?.status === 'unavailable' && <div className="pk-notice pk-notice-warn">
          <strong>本机环境不可用{environment.missing?.length ? `：缺少 ${environment.missing.join('、')}` : ''}</strong>
          {environment.problems?.map(problem => <p key={problem}>{problem}</p>)}
          <p>版本仍然是已发布状态；先到 <Link className="pk-link" to="/settings/runtime">模型与执行</Link> 定位运行环境，再重试调用。</p>
        </div>}
        {published && environment?.stale && <div className="pk-notice pk-notice-info">
          <strong>环境检查已过期</strong>
          <p>{environment.stale_reason}；发布状态不变，重新检查后再调用更稳妥。</p>
        </div>}
        {detail.bindings.length > 0 && <div className="pk-section"><h2>已挂靠的职能体</h2>
          <ul className="pk-list">{detail.bindings.map(binding => <li key={binding.id} className="pk-row">
            <Link className="pk-link" to={`/agents/${encodeURIComponent(binding.agent_id)}`}>{binding.agent_id.slice(0, 8)}</Link>
            <span className="pk-pill is-draft">固定 v{binding.version}</span></li>)}</ul></div>}
      </>}

      {tab === 'content' && <div className="pk-section">
        <h2>能力内容清单</h2>
        <p className="pk-muted">以下是该职能包包含的文件与配置。客户原始资料与任务结果默认不入包。</p>
        <ul className="pk-list">{(draft?.files ?? published?.files ?? []).map(file => <li key={file.path} className="pk-row">
          <span>{file.path}<br /><code>{file.sha256.slice(0, 12)}… · {file.size} 字节</code></span>
          <span className="pk-pill is-draft">{file.role === 'fixture' ? `测试样本 · ${file.material_scope === 'licensed' ? '已获授权' : '合成'}` : '程序'}</span>
        </li>)}</ul>
        {(draft?.excluded?.length ?? 0) > 0 && <>
          <h2>未纳入的文件</h2>
          <ul className="pk-list">{draft!.excluded.map(row => <li key={row.path} className="pk-row">
            <span>{row.path}<br /><code>{row.reason}</code></span></li>)}</ul></>}
      </div>}

      {tab === 'validation' && <>
        <div className="pk-section">
          <h2>验证结果</h2>
          {currentEvidence
            ? <p className="pk-muted">下面的记录绑定当前候选内容摘要 <code>{draft!.content_digest.slice(0, 12)}…</code>。</p>
            : <p className="pk-muted">当前候选内容尚未运行验证。修改内容会让既有验证记录失效，需要重新运行。</p>}
          {detail.can_maintain && draft && <div className="pk-actions">
            <button className="pk-button" onClick={() => void validate()} disabled={busy !== '' || !draft.manifest}>
              {busy === 'validate' ? '验证中…' : '运行验证'}</button>
            <button className="pk-button pk-button-secondary" onClick={() => void publish()}
              disabled={busy !== '' || !currentEvidence?.passed}>
              {busy === 'publish' ? '发布中…' : '发布此版本'}</button>
            {!currentEvidence?.passed && <span className="pk-muted">验证通过后可发布</span>}
          </div>}
        </div>
        {currentEvidence && <EvidenceCard evaluation={currentEvidence} />}
        {detail.evaluations.filter(item => item.id !== currentEvidence?.id).map(item => <div key={item.id} className="pk-section">
          <h2>历史验证 · {formatDate(item.created_at)}</h2>
          <p className="pk-muted">{item.summary}（对应候选 <code>{item.content_digest.slice(0, 12)}…</code>，与当前内容不同，不能用于发布）</p>
        </div>)}
      </>}

      {tab === 'versions' && <div className="pk-section">
        <h2>版本</h2>
        {detail.versions.length === 0
          ? <p className="pk-muted">尚未发布任何版本。</p>
          : <ul className="pk-list">{detail.versions.map(version => <li key={version.id} className="pk-row">
            <span>v{version.version} · {formatDate(version.created_at)}<br />
              <code>{version.content_digest.slice(0, 16)}…</code></span>
            <span className={`pk-pill ${detail.environments[version.id]?.status === 'ready' ? 'is-published' : 'is-draft'}`}>
              {ENV_LABEL[detail.environments[version.id]?.status ?? 'unchecked']}</span>
          </li>)}</ul>}
        <p className="pk-muted">升级挂靠只影响之后的新任务；正在运行的任务与既有会话保持原来的版本快照。
          回滚是把挂靠指回旧版本，不会删除新版本或改写历史。</p>
      </div>}
    </section>
  </div>
}
