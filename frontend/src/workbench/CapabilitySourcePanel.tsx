import type { CapabilitySources } from '../workspace/types'

const ORIGIN_LABEL: Record<string, string> = {
  project_module: '项目模块',
  session_skill: '会话导入',
}

const NO_RECORD_TEXT = '缺记录——该运行未记录此类信息，无法展示。'

export default function CapabilitySourcePanel({ sources }: { sources: CapabilitySources }) {
  return (
    <details className="wb-capability-sources" data-testid="capability-sources">
      <summary>能力来源摘要</summary>
      <div className="wb-capability-sources-body">
        <section aria-label="已加载" data-testid="loaded-section">
          <h4>已加载</h4>
          {sources.loaded.status === 'no_record' ? (
            <p className="wb-no-record" data-testid="loaded-no-record">{NO_RECORD_TEXT}</p>
          ) : sources.loaded.items.length === 0 ? (
            <p>本次运行未加载额外模块或技能。</p>
          ) : (
            <ul>
              {sources.loaded.items.map((item) => (
                <li key={`${item.origin}-${item.id}`}>
                  <strong>{item.name}</strong>
                  <span className="wb-origin-tag"> ({ORIGIN_LABEL[item.origin] || item.origin})</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section aria-label="已调用" data-testid="invoked-section">
          <h4>已调用</h4>
          {sources.invoked.status === 'no_record' ? (
            <p className="wb-no-record" data-testid="invoked-no-record">{NO_RECORD_TEXT}</p>
          ) : sources.invoked.items.length === 0 ? (
            <p>本次运行未调用工具。</p>
          ) : (
            <ul>
              {sources.invoked.items.map((item) => (
                <li key={item.name}>
                  <strong>{item.name}</strong>
                  <span> ×{item.count}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </details>
  )
}
