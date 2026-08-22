/**
 * 「邮箱导入」下面还分四个视图：Outlook / Hotmail / MailAPI URL 走同一个微软号池，
 * AppleMail 走本地池文件。视图选择存在配置项 `mail_import_source` 里，`mail_provider`
 * 只记到号池粒度，所以不能拿它反推视图——那样选什么都会退回 Outlook。
 */
import { Form } from 'antd'
import type { FormInstance } from 'antd'

export type MailImportSource = 'applemail' | 'outlook' | 'hotmail' | 'mailapi'

export const MAIL_IMPORT_SOURCES: MailImportSource[] = ['applemail', 'outlook', 'hotmail', 'mailapi']

export const MAIL_IMPORT_SOURCE_OPTIONS: { value: MailImportSource; label: string }[] = [
  { value: 'outlook', label: 'Outlook（微软号池）' },
  { value: 'hotmail', label: 'Hotmail（微软号池）' },
  { value: 'mailapi', label: 'MailAPI URL（邮箱----mailapi_url）' },
  { value: 'applemail', label: 'AppleMail / 小苹果' },
]

/** 后端把这些 mail_provider 值显示成「邮箱导入」 */
export const MAIL_IMPORT_PROVIDERS = ['microsoft', 'outlook', 'applemail']

const LEGACY_SOURCE_ALIASES: Record<string, MailImportSource> = { microsoft: 'outlook' }

export function isMailImportProvider(mailProvider: string): boolean {
  return MAIL_IMPORT_PROVIDERS.includes(String(mailProvider || '').trim().toLowerCase())
}

export function normalizeMailImportSource(value: unknown, mailProvider: unknown = ''): MailImportSource {
  const raw = String(value ?? '').trim().toLowerCase()
  const aliased = LEGACY_SOURCE_ALIASES[raw] ?? raw
  if ((MAIL_IMPORT_SOURCES as string[]).includes(aliased)) {
    return aliased as MailImportSource
  }
  return String(mailProvider ?? '').trim().toLowerCase() === 'applemail' ? 'applemail' : 'outlook'
}

/**
 * 读设置页表单里的 `mail_import_source`，空串表示配置还没回来。
 *
 * 设置页没有这个字段的 Form.Item——视图选择器画在「邮箱导入」卡片头上，值只由
 * setFieldsValue 写进 store。而 useWatch 默认盯的是注册过的字段（rc-field-form 里
 * 是 getFieldsValue()），不带 preserve 就永远读到 undefined：库里明明存着 mailapi，
 * 界面照样退回 Outlook。
 */
export function useStoredMailImportSource(form: FormInstance): string {
  const watched = Form.useWatch('mail_import_source', { form, preserve: true })
  return String(watched ?? '').trim().toLowerCase()
}

export function resolveEffectiveMailProvider(mailProvider: string, mailImportSource: unknown): string {
  if (mailProvider !== 'mail_import') return mailProvider
  return normalizeMailImportSource(mailImportSource) === 'applemail' ? 'applemail' : 'microsoft'
}
