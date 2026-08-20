import type { ICloudRegion } from '@/api/icloud'

export const DEFAULT_ICLOUD_IMAP_HOST = 'imap.mail.me.com'
export const DEFAULT_ICLOUD_IMAP_PORT = 993

/** Apple 对每个主号限制每滚动小时最多成功生成 5 个隐私邮箱。 */
export const ICLOUD_HOURLY_ALIAS_LIMIT = 5

export const ICLOUD_REGION_OPTIONS: { value: ICloudRegion; label: string }[] = [
  { value: 'global', label: '国际版（icloud.com）' },
  { value: 'china', label: '中国大陆（icloud.com.cn）' },
]

export function getICloudRegionLabel(region?: string): string {
  return ICLOUD_REGION_OPTIONS.find((item) => item.value === region)?.label ?? region ?? '-'
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '-' : parsed.toLocaleString()
}
