/** Shared delivery-type labels used by both RunWorkspace and Deliverables. */

export type DeliveryType = 'service' | 'cli' | 'installer' | null

export const DELIVERY_TYPE_LABEL: Record<string, string> = {
  service: '线上服务',
  cli: '命令行工具',
  installer: '安装包',
}
