import { Alert, Card, Result } from 'antd'
import { C } from '../theme/tokens'

/**
 * 「本域无此概念」页。
 *
 * 移植 OA 复刻件那条最好的设计（要点 #5）：无权限 / 请求失败 / 确实无数据
 * 是三种必须分开呈现的状态。这里补第四种 —— **这个指标在本系统不存在**。
 *
 * 为什么不直接把这个页签删掉：源界面有 SLA 页，看过那套界面的人会去找它。
 * 找不到会以为是自己点错了，或者以为功能坏了。明确写「没有这个概念，
 * 因为没有承诺时限」比让人猜有用。这也是唯一不该造假数据的地方 ——
 * 编一个 SLA 达成率填进去，会让人拿它做决策。
 */
export default function NotAvailable({
  title,
  why,
  instead,
}: {
  title: string
  why: string
  instead: string
}) {
  return (
    <Card size="small">
      <Result
        status="info"
        title={`${title} · 本系统不适用`}
        subTitle={
          <div style={{ fontSize: 12, color: C.textSub, lineHeight: 2, textAlign: 'left', maxWidth: 620, margin: '0 auto' }}>
            <div>{why}</div>
            <div style={{ marginTop: 8 }}>替代看法：{instead}</div>
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 14, fontSize: 11, textAlign: 'left' }}
              message="这一页刻意留空，不填数字"
              description="源界面在这里有内容，所以页签保留着，免得看过那套界面的人以为功能坏了。但工厂里没有对应概念，硬造一个达成率会被拿去做决策 —— 空着并说明原因，比填一个看起来合理的假值安全。"
            />
          </div>
        }
      />
    </Card>
  )
}
