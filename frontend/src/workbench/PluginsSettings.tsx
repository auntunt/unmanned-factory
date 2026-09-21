import { useCallback, useEffect, useState } from 'react'

import { request, WorkspaceApiError } from '../workspace/api'
import { EmptyState, ErrorNotice, PageHeader, errorText, formatDate } from './ui'
import type { PageProps } from './ui'
import type { PluginAvailabilityView, PluginState } from './maintenance-types'
import { pluginStateLabel } from './maintenance-types'

/** 业务插件启停。
 *
 *  这个页面不安装、不上传、不删除任何东西——插件清单在发行包里是固定的，
 *  管理员唯一能改的是「还接不接活」。停用必须先经过排空，这条规则在服务端，
 *  这里只是把服务端的拒绝原样显示出来，不自己再判一遍。
 */

const NEXT_STATES: Record<PluginState, { to: PluginState; label: string; hint: string }[]> = {
  enabled: [{
    to: 'draining', label: '开始排空',
    hint: '不再接受新任务，已经在跑的按原授权继续。',
  }],
  draining: [
    { to: 'enabled', label: '重新启用', hint: '恢复接受新任务。' },
    { to: 'disabled', label: '停用', hint: '活跃执行清空后才会成功；不会取消客户任务。' },
  ],
  // 两个新接入的场景出厂就是停用的，从没被启用过，所以这里不能说「重新启用」。
  disabled: [{
    to: 'enabled', label: '启用',
    hint: '历史任务不会自动重启，需要继续的任务由人再发起。',
  }],
}

function StateBadge({ state }: { state: PluginState }) {
  const tone = state === 'enabled' ? 'success' : state === 'draining' ? 'warning' : 'neutral'
  return (
    <span className={`wb-status wb-status-${tone}`}>
      <i aria-hidden="true" />{pluginStateLabel(state)}
    </span>
  )
}

function PluginCard({ plugin, csrfToken, onUnauthorized, onChanged }: PageProps & {
  plugin: PluginAvailabilityView
  onChanged: (next: PluginAvailabilityView) => void
}) {
  const [busy, setBusy] = useState<PluginState | null>(null)
  const [error, setError] = useState<string | null>(null)

  const move = async (to: PluginState) => {
    setBusy(to)
    setError(null)
    try {
      // 带上读到的 revision：两个管理员同时操作时，后一个会被服务端拒绝，
      // 而不是无声地盖掉前一个的决定。
      const next = await request<PluginAvailabilityView>(
        `/api/v2/plugins/${encodeURIComponent(plugin.id)}/state`,
        {
          method: 'POST', csrfToken, onUnauthorized,
          body: { state: to, expected_revision: plugin.revision },
        },
      )
      onChanged(next)
    } catch (cause) {
      if (cause instanceof WorkspaceApiError && cause.status === 401) return
      setError(errorText(cause))
    } finally {
      setBusy(null)
    }
  }

  const transitions = NEXT_STATES[plugin.state]
  return (
    <div className="wb-card" data-testid={`plugin-${plugin.id}`}>
      <div className="wb-card-head">
        <h3>{plugin.name} <StateBadge state={plugin.state} /></h3>
        <p className="wb-muted">
          方法包 {plugin.skill_pack_id} · 插件 v{plugin.version} · 宿主契约 {plugin.contract.min}–{plugin.contract.max}
          （当前宿主 {plugin.contract.host}）
        </p>
      </div>

      {!plugin.executable && (
        <p className="wb-notice" role="status">
          尚未接入可执行处理器，不能启用。这里登记的是声明，不是已经就绪的入口。
        </p>
      )}

      {plugin.executable && (
        <div className="wb-fact-grid">
          <div>
            <span>业务入口</span>
            <strong>{Object.keys(plugin.entry_points).join(' / ') || '（无）'}</strong>
          </div>
          <div>
            <span>所需宿主能力</span>
            <strong>{plugin.host_capabilities.length} 项</strong>
          </div>
          <div>
            <span>最近变更</span>
            <strong>{plugin.updated_at ? formatDate(plugin.updated_at) : '未变更过'}</strong>
          </div>
          <div>
            <span>操作人</span>
            <strong>{plugin.actor}</strong>
          </div>
        </div>
      )}

      {error && <ErrorNotice message={error} />}

      <div className="wb-form-actions">
        {transitions.map(t => (
          <button
            key={t.to}
            className={`wb-button ${t.to === 'enabled' ? 'wb-button-primary' : 'wb-button-secondary'}`}
            disabled={busy !== null || (t.to === 'enabled' && !plugin.executable)}
            title={t.hint}
            onClick={() => move(t.to)}
          >
            {busy === t.to ? '处理中…' : t.label}
          </button>
        ))}
      </div>
      <p className="wb-muted">{transitions.map(t => t.hint).join(' ')}</p>
    </div>
  )
}

export default function PluginsSettings({ csrfToken, onUnauthorized, user }: PageProps) {
  const [plugins, setPlugins] = useState<PluginAvailabilityView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const isAdmin = user?.role !== 'member'

  const load = useCallback((signal?: AbortSignal) => {
    request<{ plugins: PluginAvailabilityView[] }>('/api/v2/plugins', { onUnauthorized, signal })
      .then(result => { if (!signal?.aborted) setPlugins(result.plugins) })
      .catch(cause => {
        if (signal?.aborted) return
        if (cause instanceof WorkspaceApiError && cause.status === 401) return
        setError(errorText(cause))
      })
  }, [onUnauthorized])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  return (
    <div className="wb-page">
      <PageHeader
        title="业务插件"
        description="启停随发行包一起分发的受信任业务插件。这里不安装也不上传代码，只决定它还接不接新任务。"
      />
      {error && <ErrorNotice message={error} />}
      {!isAdmin && (
        <div className="wb-notice" role="status">
          只有管理员可以改变插件启停；下面是当前状态。
        </div>
      )}
      {plugins && plugins.length === 0 && (
        <div className="wb-card">
          <EmptyState title="没有登记的业务插件" description="发行包里没有声明任何业务插件。" />
        </div>
      )}
      {(plugins ?? []).map(plugin => (
        isAdmin
          ? <PluginCard
              key={plugin.id} plugin={plugin} csrfToken={csrfToken}
              onUnauthorized={onUnauthorized} user={user}
              onChanged={next => setPlugins(current =>
                (current ?? []).map(p => (p.id === next.id ? next : p)))}
            />
          : <div className="wb-card" key={plugin.id} data-testid={`plugin-${plugin.id}`}>
              <h3>{plugin.name} <StateBadge state={plugin.state} /></h3>
              <p className="wb-muted">方法包 {plugin.skill_pack_id} · 插件 v{plugin.version}</p>
            </div>
      ))}
    </div>
  )
}
