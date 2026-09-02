import { Form, Input, InputNumber, Select, Space, Typography } from 'antd'
import type { PaymentCard, PaymentField } from '@/api/payments'

const { Text } = Typography

interface PaymentOptionsFormProps {
  fields: PaymentField[]
  values: Record<string, unknown>
  onChange: (key: string, value: unknown) => void
  cards?: PaymentCard[]
}

export function PaymentOptionsForm({ fields, values, onChange, cards = [] }: PaymentOptionsFormProps) {
  const visibleFields = fields.filter((field) => !field.advanced)
  const advancedFields = fields.filter((field) => field.advanced)

  const renderField = (field: PaymentField) => {
    const value = values[field.key]
    let control: React.ReactNode
    if (field.control === 'select') {
      control = (
        <Select
          value={value as string | undefined}
          onChange={(next) => onChange(field.key, next)}
          options={(field.options || []).map((option) => ({ value: option, label: option }))}
          style={{ width: '100%' }}
        />
      )
    } else if (field.control === 'number') {
      control = <InputNumber value={value as number | undefined} onChange={(next) => onChange(field.key, next)} min={1} style={{ width: '100%' }} />
    } else if (field.control === 'card') {
      control = (
        <Select
          value={value as number | undefined}
          onChange={(next) => onChange(field.key, next)}
          placeholder="选择支付卡片"
          options={cards.map((card) => ({
            value: card.id,
            disabled: card.max_uses > 0 && card.uses >= card.max_uses,
            label: (
              <Space>
                <Text>{card.brand || 'CARD'} •••• {card.last4}</Text>
                <Text type="secondary">{card.uses}/{card.max_uses}</Text>
              </Space>
            ),
          }))}
          style={{ width: '100%' }}
        />
      )
    } else {
      control = <Input value={String(value || '')} onChange={(event) => onChange(field.key, event.target.value)} placeholder={field.key.endsWith('_proxy') ? '留空自动选择' : undefined} />
    }
    return (
      <Form.Item key={field.key} label={field.label} required={field.required} style={{ marginBottom: 14 }}>
        {control}
      </Form.Item>
    )
  }

  return (
    <>
      {visibleFields.map(renderField)}
      {advancedFields.length > 0 ? (
        <details style={{ marginTop: 8 }}>
          <summary style={{ color: 'var(--text-secondary)', cursor: 'pointer', marginBottom: 12 }}>高级设置</summary>
          {advancedFields.map(renderField)}
        </details>
      ) : null}
    </>
  )
}
