import { useEffect, useState } from 'react'

import { WorkspaceApiError } from '../workspace/api'
import { ErrorNotice, PageHeader, errorText } from '../workbench/ui'
import type { PageProps } from '../workbench/ui'
import { maintenanceApi } from './api'
import { CONTRACT_VERSION } from './types'

type ManifestShape = {
  name?: string
  component?: string
  version?: string | number
  contract_version?: string
  supports_pause?: boolean
  actions?: string[]
  entry_points?: Record<string, unknown>
  cli?: Record<string, unknown> | string
  embedding?: Record<string, unknown> | string
  [key: string]: unknown
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((v): v is string => typeof v === 'string')
}

export default function AboutPage({ onUnauthorized }: PageProps) {
  const [manifest, setManifest] = useState<ManifestShape | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    maintenanceApi.manifest({ onUnauthorized, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setManifest(result as ManifestShape) })
      .catch(cause => {
        if (controller.signal.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
    return () => controller.abort()
  }, [onUnauthorized])

  const actions = asStringList(manifest?.actions)

  return (
    <div className="wb-page">
      <PageHeader title="技术详情" description="子系统组件信息、契约版本与 CLI 接法；这里不是主要工作入口。" />

      {error && <ErrorNotice message={`宿主清单读取失败：${error}`} />}
      {!manifest && !error && <div className="wb-card"><div className="wb-list-placeholder"><span /><span /></div></div>}

      {manifest && (
        <>
          <section className="wb-card" aria-label="组件信息">
            <div className="wb-card-head"><div><span className="wb-eyebrow">组件</span><h2>{String(manifest.name ?? manifest.component ?? '运维维护子系统')}</h2></div></div>
            <div className="wb-form-grid wb-form-grid-two">
              <div><span className="wb-eyebrow">组件版本</span><p>{manifest.version != null ? String(manifest.version) : '—'}</p></div>
              <div><span className="wb-eyebrow">契约版本</span><p>{manifest.contract_version ?? CONTRACT_VERSION}</p></div>
              <div className="wb-span-two">
                <span className="wb-eyebrow">原地暂停</span>
                <p>{manifest.supports_pause === false ? '不支持原地暂停' : manifest.supports_pause ? '支持' : '不支持原地暂停'}</p>
              </div>
            </div>
          </section>

          <section className="wb-card" aria-label="支持的动作" style={{ marginTop: 18 }}>
            <div className="wb-card-head"><div><span className="wb-eyebrow">动作</span><h2>支持的动作</h2></div></div>
            {actions.length > 0
              ? <ul className="ms-side-list">{actions.map(a => <li key={a}>{a}</li>)}</ul>
              : <p className="wb-runtime-note">宿主清单未返回动作列表。</p>}
          </section>

          <section className="wb-card" aria-label="CLI 用法" style={{ marginTop: 18 }}>
            <div className="wb-card-head"><div><span className="wb-eyebrow">CLI</span><h2>webuddy-maintenance</h2></div></div>
            <ul className="ms-side-list">
              <li>本地数据域：<code>--data-dir</code>（与 webuddy 服务共用同一 control.db 时看到同一对象）</li>
              <li>最小运行时：<code>webuddy-maintenance runtime</code> 在无网页的情况下持有执行器锁并真实执行队列</li>
              <li>完整安装说明见 <code>docs/maintenance-subsystem/INSTALL.md</code></li>
            </ul>
          </section>

          <section className="wb-card" aria-label="嵌入方式" style={{ marginTop: 18 }}>
            <div className="wb-card-head"><div><span className="wb-eyebrow">嵌入</span><h2>嵌入契约摘要</h2></div></div>
            <ul className="ms-side-list">
              <li>子系统提供独立入口 <code>/embed/maintenance/*</code>，不渲染主外框，供其他宿主内嵌</li>
              <li>宿主需提供：身份、项目权限、执行器配置、数据目录</li>
              <li>终端与网页只有连接同一数据域时才看到同一任务，不承诺跨安装自动同步</li>
            </ul>
          </section>
        </>
      )}
    </div>
  )
}
