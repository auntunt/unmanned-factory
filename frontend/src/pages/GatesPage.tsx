import { Alert, Card, Empty, Spin, Table, Tag, Tooltip } from 'antd'
import { C, MONO } from '../theme/tokens'
import { fmtRate } from '../theme/geometry'

/**
 * 判据闸门页 —— 对位 OA 的「闸门学习」页。
 *
 * 那一页最值钱的设计不是表格，是系统主动声明自己指标的系统性偏差
 * （「回放样本全是正样本，任何放宽都会显得无代价」）。后端 _gates()
 * 已经给了同构的 caveat（precision 因未定案而系统性偏高），这里必须把它
 * 摆在表格**上方**而不是脚注 —— 放下面等于没写，人看完表就走了。
 */

interface GateRow {
  gate_id: string
  role: string
  gate: string
  fired: number
  true_positives: number
  false_positives: number
  unadjudicated: number
  precision: number | null
  verdict: string
}

interface Props {
  analytics: any
  loading: boolean
}

export default function GatesPage({ analytics, loading }: Props) {
  if (loading && !analytics) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  const gates = analytics?.gates
  if (!gates) {
    return (
      <Card size="small">
        <Empty description="判据数据读不到" />
      </Card>
    )
  }

  const rows: GateRow[] = gates.rows ?? []
  const neverFired: number = gates.never_fired ?? 0
  const caveat = gates.caveat

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {caveat && (
        <Alert
          // reliable=false 时用 error：这张表此刻不能拿来做裁剪判据的决策
          type={caveat.reliable ? 'info' : 'error'}
          showIcon
          message={
            caveat.reliable
              ? 'precision 有已知偏差，但样本量尚可参考'
              : '这张表现在不能用来做删判据的决策'
          }
          description={
            <div style={{ fontSize: 12, color: C.textSub, lineHeight: 1.9 }}>
              {caveat.text}
              {caveat.unadjudicated != null && (
                <div style={{ marginTop: 6 }}>
                  当前未定案触发 <b>{caveat.unadjudicated}</b> 次 · 已定案{' '}
                  <b>{caveat.adjudicated ?? 0}</b> 次
                  {caveat.unadjudicated > (caveat.adjudicated ?? 0) && (
                    <Tag bordered={false} color="warning" style={{ marginLeft: 8, fontSize: 11 }}>
                      未定案多于已定案，precision 偏高更严重
                    </Tag>
                  )}
                </div>
              )}
            </div>
          }
        />
      )}

      {neverFired > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${neverFired} / ${rows.length} 道判据从未触发过`}
          description="从未触发有两种可能：这类问题真没发生过，或者判据写错了根本匹配不上。两者在数据上长得一样，只能人去看代码。这些行已排在表格最前面。"
        />
      )}

      <Card size="small" title={`判据闸门（${rows.length} 道）`}>
        <Table<GateRow>
          size="small"
          rowKey="gate_id"
          dataSource={rows}
          pagination={false}
          scroll={{ x: 900, y: 520 }}
          rowClassName={(r) => (r.fired === 0 ? 'gate-never-fired' : '')}
          columns={[
            {
              title: '判据',
              dataIndex: 'gate',
              width: 260,
              render: (v: string, r) => (
                <Tooltip title={r.gate_id}>
                  <span style={{ fontFamily: MONO, fontSize: 12 }}>{v}</span>
                </Tooltip>
              ),
            },
            {
              title: '角色',
              dataIndex: 'role',
              width: 96,
              render: (v: string) => (
                <Tag bordered={false} style={{ fontSize: 11 }}>
                  {v}
                </Tag>
              ),
            },
            {
              title: '触发',
              dataIndex: 'fired',
              width: 72,
              align: 'right',
              sorter: (a, b) => a.fired - b.fired,
              render: (v: number) =>
                v === 0 ? (
                  <Tag bordered={false} color="warning" style={{ fontSize: 11 }}>
                    从未
                  </Tag>
                ) : (
                  <span style={{ fontFamily: MONO }}>{v}</span>
                ),
            },
            {
              title: '真阳',
              dataIndex: 'true_positives',
              width: 66,
              align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO, color: C.success }}>{v}</span>,
            },
            {
              title: '假阳',
              dataIndex: 'false_positives',
              width: 66,
              align: 'right',
              render: (v: number) => (
                <span style={{ fontFamily: MONO, color: v > 0 ? C.error : C.textDisabled }}>{v}</span>
              ),
            },
            {
              title: '未定案',
              dataIndex: 'unadjudicated',
              width: 80,
              align: 'right',
              render: (v: number) => (
                <Tooltip title={v > 0 ? '这些触发没人定案，既不进 precision 的分子也不进分母' : ''}>
                  <span style={{ fontFamily: MONO, color: v > 0 ? C.warning : C.textDisabled }}>
                    {v}
                  </span>
                </Tooltip>
              ),
            },
            {
              title: 'precision',
              dataIndex: 'precision',
              width: 104,
              align: 'right',
              render: (v: number | null, r) =>
                v == null ? (
                  <Tooltip title="没有已定案的触发，算不出 precision。显示 0% 会被读成「全是误报」。">
                    <span style={{ color: C.textDisabled }}>—</span>
                  </Tooltip>
                ) : (
                  <Tooltip
                    title={
                      r.unadjudicated > 0
                        ? `基于 ${r.true_positives + r.false_positives} 个已定案样本，另有 ${r.unadjudicated} 个未定案未计入`
                        : '全部触发均已定案'
                    }
                  >
                    <Tag
                      bordered={false}
                      color={v >= 0.8 ? 'success' : v >= 0.5 ? 'warning' : 'error'}
                      style={{ fontFamily: MONO, fontSize: 11 }}
                    >
                      {fmtRate(v)}
                      {r.unadjudicated > 0 && '*'}
                    </Tag>
                  </Tooltip>
                ),
            },
            {
              title: '结论',
              dataIndex: 'verdict',
              ellipsis: true,
              render: (v: string) => (
                <span style={{ fontSize: 12, color: C.textSub }}>{v}</span>
              ),
            },
          ]}
        />
        <div style={{ marginTop: 8, fontSize: 11, color: C.textDisabled }}>
          带 * 的 precision 存在未定案样本，实际值可能低于显示值。
        </div>
      </Card>

      <Card size="small" title="按监工角色汇总">
        <Table
          size="small"
          rowKey="role"
          dataSource={gates.roles ?? []}
          pagination={false}
          columns={[
            { title: '监工', dataIndex: 'role', width: 130,
              render: (v: string) => <Tag bordered={false} color="purple" style={{ fontSize: 11 }}>{v}</Tag> },
            { title: '触发', dataIndex: 'fired', width: 72, align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO }}>{v}</span> },
            { title: '放行', dataIndex: 'passed', width: 72, align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO }}>{v}</span> },
            { title: '真阳', dataIndex: 'true_positives', width: 66, align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO, color: C.success }}>{v}</span> },
            { title: '假阳', dataIndex: 'false_positives', width: 66, align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO, color: v > 0 ? C.error : C.textDisabled }}>{v}</span> },
            { title: '未定案', dataIndex: 'unadjudicated', width: 80, align: 'right',
              render: (v: number) => <span style={{ fontFamily: MONO, color: v > 0 ? C.warning : C.textDisabled }}>{v}</span> },
            { title: 'precision', dataIndex: 'precision', width: 100, align: 'right',
              render: (v: number | null) => v == null
                ? <span style={{ color: C.textDisabled }}>—</span>
                : <span style={{ fontFamily: MONO }}>{fmtRate(v)}</span> },
            { title: '结论', dataIndex: 'verdict', ellipsis: true,
              render: (v: string) => <span style={{ fontSize: 12, color: C.textSub }}>{v}</span> },
          ]}
        />
      </Card>
    </div>
  )
}
