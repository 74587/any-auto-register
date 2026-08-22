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

/**
 * 导出格式沿用仓库里邮箱池导入那套 `----` 分隔：
 *   mail_url → 隐私邮箱----邮件 URL（默认，正好是邮箱导入里 `邮箱----mailapi_url` 那一行）
 *   account  → 隐私邮箱----所属主号（想知道每个别名挂在哪个 Apple ID 下时用）
 */
export type AliasExportMode = 'mail_url' | 'account'

export const ALIAS_EXPORT_FILENAME = 'icloud_aliases.txt'

export interface AliasExportRecord {
  address: string
  account_email: string
  share_token?: string
}

/**
 * 补 share_token 之前建的老别名没有免登录链接，这种行导不出 URL，只能整行跳过：
 * 写一行末尾空着的 `邮箱----` 反而会让对面的导入器报格式错。
 */
export function formatAliasExport(
  aliases: AliasExportRecord[],
  mode: AliasExportMode = 'mail_url',
): string {
  const lines: string[] = []
  for (const alias of aliases) {
    if (mode === 'account') {
      lines.push(`${alias.address}----${alias.account_email}`)
      continue
    }
    const url = aliasMailUrl(String(alias.share_token || ''))
    if (url) lines.push(`${alias.address}----${url}`)
  }
  return lines.join('\n')
}

/** 导出前先算能导出几条，好在没有链接的别名被跳过时如实告诉用户。 */
export function countExportableAliases(
  aliases: AliasExportRecord[],
  mode: AliasExportMode = 'mail_url',
): number {
  if (mode === 'account') return aliases.length
  return aliases.filter((alias) => String(alias.share_token || '').trim()).length
}

export function downloadTextFile(filename: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * 隐私邮箱的免登录邮件链接：打开就是这个地址的最新一封邮件正文。
 * 链接本身就是权限，复制给谁谁都能看，所以走的是后端那串随机 share_token。
 */
export function aliasMailUrl(shareToken: string): string {
  return shareToken ? `${window.location.origin}/m/${shareToken}` : ''
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '-' : parsed.toLocaleString()
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** 邮件列表里用相对时间，扫起来比一串完整时间戳快得多。 */
export function formatRelativeTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  const diff = Date.now() - parsed.getTime()
  if (diff < 0) return parsed.toLocaleDateString()
  if (diff < MINUTE) return '刚刚'
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)} 天前`
  return parsed.toLocaleDateString()
}
