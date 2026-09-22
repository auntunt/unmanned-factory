/** Shown wherever an object was explicitly declared synthetic (合成/演示) at
 *  registration or by its intake source. Never inferred from content. */
export default function SyntheticBadge({ synthetic }: { synthetic?: boolean | null }) {
  if (!synthetic) return null
  return <span className="wb-status ms-synthetic" title="登记或接入来源明确声明为合成/演示数据，不是客户交付">合成</span>
}
