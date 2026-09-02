import { apiFetch } from '@/lib/utils'

export type PaymentOperation = 'link' | 'pay'

export interface PaymentField {
  key: string
  label: string
  control: 'text' | 'number' | 'select' | 'card'
  required?: boolean
  default?: string | number
  options?: string[]
  advanced?: boolean
}

export interface PaymentChannel {
  name: string
  display_name: string
  operations: PaymentOperation[]
  option_schema: Record<PaymentOperation, PaymentField[]>
}

export interface PaymentCard {
  id: number
  brand: string
  last4: string
  name: string
  source: string
  uses: number
  max_uses: number
  note: string
}

export interface PaymentResult {
  account_id: number
  email: string
  ok: boolean
  channel: string
  operation: string
  link?: string
  checkout_session_id?: string
  billing_country?: string
  subscription_plan?: string
  card_last4?: string
  error?: string
}

export interface PaymentTask {
  task_id: string
  total: number
}

export async function getPaymentSettings(): Promise<{ linkProxy: string; payProxy: string }> {
  const data = await apiFetch('/config') as {
    payment_link_proxy?: string
    payment_pay_proxy?: string
    payment_proxy?: string
  }
  const legacy = String(data.payment_proxy || '')
  return {
    linkProxy: String(data.payment_link_proxy || legacy),
    payProxy: String(data.payment_pay_proxy || legacy),
  }
}

export async function savePaymentProxy(kind: 'link' | 'pay', proxy: string): Promise<void> {
  const key = kind === 'link' ? 'payment_link_proxy' : 'payment_pay_proxy'
  await apiFetch('/config', {
    method: 'PUT',
    body: JSON.stringify({ data: { [key]: proxy } }),
  })
}

export async function listPaymentChannels(): Promise<PaymentChannel[]> {
  const data = await apiFetch('/payments/channels') as { channels?: PaymentChannel[] }
  return data.channels || []
}

export async function listPaymentCards(channel = 'direct'): Promise<PaymentCard[]> {
  const data = await apiFetch(`/payments/channels/${channel}/cards`) as { cards?: PaymentCard[] }
  return data.cards || []
}

export async function addPaymentCard(channel: string, body: Record<string, unknown>): Promise<PaymentCard> {
  const data = await apiFetch(`/payments/channels/${channel}/cards`, {
    method: 'POST',
    body: JSON.stringify(body),
  }) as { card: PaymentCard }
  return data.card
}

export async function deletePaymentCard(channel: string, cardId: number): Promise<void> {
  await apiFetch(`/payments/channels/${channel}/cards/${cardId}`, { method: 'DELETE' })
}

export async function resetPaymentCardUses(channel = 'direct'): Promise<void> {
  await apiFetch(`/payments/channels/${channel}/cards/reset-uses`, { method: 'POST' })
}

export async function createPaymentTask(body: {
  account_ids: number[]
  operation: PaymentOperation
  channel: string
  options: Record<string, unknown>
  concurrency?: number
  delay_seconds?: number
}): Promise<PaymentTask> {
  return apiFetch('/payments/jobs', {
    method: 'POST',
    body: JSON.stringify(body),
  }) as Promise<PaymentTask>
}

export async function runPayment(accountId: number, operation: PaymentOperation, channel: string, options: Record<string, unknown>) {
  return apiFetch(`/payments/${accountId}/${operation}`, {
    method: 'POST',
    body: JSON.stringify({ channel, options }),
  }) as Promise<{ ok: boolean; data?: Record<string, unknown>; error?: string; channel: string; operation: string }>
}
