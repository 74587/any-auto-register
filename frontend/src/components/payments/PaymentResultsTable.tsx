import { Button, Empty, Space, Table, Tag, Typography } from 'antd'
import { CopyOutlined, LinkOutlined } from '@ant-design/icons'
import type { PaymentResult } from '@/api/payments'

const { Text } = Typography

export function PaymentResultsTable({ results }: { results: PaymentResult[] }) {
  if (results.length === 0) return <Empty description="暂无执行结果" image={Empty.PRESENTED_IMAGE_SIMPLE} />

  const copy = async (value: string) => {
    await navigator.clipboard.writeText(value)
  }

  return (
    <Table
      size="small"
      rowKey={(record) => `${record.account_id}-${record.operation}`}
      dataSource={results}
      pagination={{ pageSize: 10, showSizeChanger: false }}
      scroll={{ x: 720 }}
      columns={[
        { title: '账号', dataIndex: 'email', ellipsis: true, width: 220 },
        { title: '渠道', dataIndex: 'channel', width: 80, render: (value: string) => <Tag>{value}</Tag> },
        {
          title: '状态', dataIndex: 'ok', width: 90,
          render: (value: boolean) => <Tag color={value ? 'success' : 'error'}>{value ? '成功' : '失败'}</Tag>,
        },
        {
          title: '详情', key: 'detail', width: 220,
          render: (_: unknown, record: PaymentResult) => record.ok ? (
            <Space size={8}>
              {record.subscription_plan ? <Text>{record.subscription_plan}</Text> : null}
              {record.card_last4 ? <Text type="secondary">卡 •••• {record.card_last4}</Text> : null}
              {record.link ? (
                <Space size={0}>
                  <Button type="text" size="small" icon={<CopyOutlined />} aria-label="复制链接" onClick={() => copy(record.link || '')} />
                  <Button type="text" size="small" icon={<LinkOutlined />} aria-label="打开链接" onClick={() => window.open(record.link, '_blank', 'noopener,noreferrer')} />
                </Space>
              ) : null}
            </Space>
          ) : <Text type="danger" ellipsis={{ tooltip: record.error }}>{record.error || '执行失败'}</Text>,
        },
      ]}
    />
  )
}
