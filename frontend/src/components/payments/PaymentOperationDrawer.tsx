import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Drawer, Segmented, Space, Spin, Typography, message } from 'antd'
import { CreditCardOutlined, LinkOutlined, PlayCircleOutlined } from '@ant-design/icons'
import {
  listPaymentCards,
  listPaymentChannels,
  getPaymentSettings,
  runPayment,
  savePaymentProxy,
  type PaymentChannel,
  type PaymentCard,
  type PaymentOperation,
  type PaymentResult,
} from '@/api/payments'
import { PaymentOptionsForm } from './PaymentOptionsForm'
import { PaymentResultsTable } from './PaymentResultsTable'

const { Text, Title } = Typography

interface PaymentOperationDrawerProps {
  account: { id: number; email: string } | null
  open: boolean
  onClose: () => void
  onDone?: () => void
}

function initialValues(channel: PaymentChannel | undefined, operation: PaymentOperation) {
  const values: Record<string, unknown> = {}
  for (const field of channel?.option_schema?.[operation] || []) {
    if (field.default !== undefined) values[field.key] = field.default
  }
  return values
}

function proxyKey(operation: PaymentOperation) {
  return operation === 'link' ? 'link_proxy' : 'pay_proxy'
}

export function PaymentOperationDrawer({ account, open, onClose, onDone }: PaymentOperationDrawerProps) {
  const [channels, setChannels] = useState<PaymentChannel[]>([])
  const [cards, setCards] = useState<PaymentCard[]>([])
  const [channelName, setChannelName] = useState('direct')
  const [operation, setOperation] = useState<PaymentOperation>('link')
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [paymentProxies, setPaymentProxies] = useState({ link: '', pay: '' })
  const proxySaveTimer = useRef<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<PaymentResult | null>(null)

  const channel = useMemo(() => channels.find((item) => item.name === channelName), [channels, channelName])

  useEffect(() => {
    if (!open) return
    setLoading(true)
    Promise.all([listPaymentChannels(), listPaymentCards('direct'), getPaymentSettings()])
      .then(([nextChannels, nextCards, settings]) => {
        setChannels(nextChannels)
        setCards(nextCards)
        const first = nextChannels[0]
        const nextChannel = first?.name || 'direct'
        setChannelName(nextChannel)
        setOperation(first?.operations?.includes('link') ? 'link' : 'pay')
        const nextOperation: PaymentOperation = first?.operations?.includes('link') ? 'link' : 'pay'
        setPaymentProxies({ link: settings.linkProxy, pay: settings.payProxy })
        setValues({ ...initialValues(first, nextOperation), [proxyKey(nextOperation)]: nextOperation === 'link' ? settings.linkProxy : settings.payProxy })
      })
      .catch((error) => message.error(error instanceof Error ? error.message : '加载支付配置失败'))
      .finally(() => setLoading(false))
  }, [open])

  useEffect(() => () => {
    if (proxySaveTimer.current !== null) window.clearTimeout(proxySaveTimer.current)
  }, [])

  useEffect(() => {
    if (channels.length > 0) setValues({ ...initialValues(channel, operation), [proxyKey(operation)]: operation === 'link' ? paymentProxies.link : paymentProxies.pay })
  }, [channelName, operation, channels, channel])

  const submit = async () => {
    if (!account) return
    const required = (channel?.option_schema?.[operation] || []).filter((field) => field.required)
    const missing = required.find((field) => values[field.key] === undefined || values[field.key] === '')
    if (missing) {
      message.error(`请填写${missing.label}`)
      return
    }
    setRunning(true)
    try {
      const response = await runPayment(account.id, operation, channelName, values)
      const data = response.data || {}
      const nextResult: PaymentResult = {
        account_id: account.id,
        email: account.email,
        ok: response.ok,
        channel: response.channel,
        operation: response.operation,
        ...data,
        error: response.error,
      }
      setResult(nextResult)
      if (response.ok) {
        message.success(operation === 'link' ? '支付链接已生成' : '支付已完成')
        onDone?.()
      } else {
        message.error(response.error || '操作失败')
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '请求失败')
    } finally {
      setRunning(false)
    }
  }

  const operationOptions = channel?.operations?.map((item) => ({
    value: item,
    label: item === 'link' ? <><LinkOutlined /> 提链</> : <><CreditCardOutlined /> 支付</>,
  })) || []

  return (
    <Drawer
      title={<Space><CreditCardOutlined /><span>{operation === 'link' ? '提链' : '支付'} · {account?.email}</span></Space>}
      open={open}
      onClose={onClose}
      width={460}
      destroyOnClose
      extra={<Button type="primary" icon={<PlayCircleOutlined />} loading={running} onClick={submit}>执行</Button>}
    >
      {loading ? <Spin /> : (
        <Space direction="vertical" size={18} style={{ width: '100%' }}>
          <div>
            <Text type="secondary">操作</Text>
            <Segmented
              block
              value={operation}
              options={operationOptions}
              onChange={(value) => setOperation(value as PaymentOperation)}
              style={{ marginTop: 8 }}
            />
          </div>
          <div>
            <Text type="secondary">支付渠道</Text>
            <Segmented
              block
              value={channelName}
              options={channels.map((item) => ({ value: item.name, label: item.display_name }))}
              onChange={(value) => setChannelName(String(value))}
              style={{ marginTop: 8 }}
            />
          </div>
          {operation === 'pay' && cards.length === 0 ? <Alert type="warning" showIcon message="暂无可用卡片，请先在支付中心的渠道管理中添加" /> : null}
          <PaymentOptionsForm
            fields={channel?.option_schema?.[operation] || []}
            values={values}
            cards={cards}
            onChange={(key, value) => {
              setValues((previous) => ({ ...previous, [key]: value }))
              if ((key === 'link_proxy' || key === 'pay_proxy') && channelName === 'direct') {
                const proxy = String(value || '')
                const kind = key === 'link_proxy' ? 'link' : 'pay'
                setPaymentProxies((previous) => ({ ...previous, [kind]: proxy }))
                if (proxySaveTimer.current !== null) window.clearTimeout(proxySaveTimer.current)
                proxySaveTimer.current = window.setTimeout(() => {
                  savePaymentProxy(kind, proxy).catch(() => {})
                }, 500)
              }
            }}
          />
          {result ? (
            <div>
              <Title level={5} style={{ margin: '4px 0 10px' }}>最近结果</Title>
              <PaymentResultsTable results={[result]} />
            </div>
          ) : null}
        </Space>
      )}
    </Drawer>
  )
}
