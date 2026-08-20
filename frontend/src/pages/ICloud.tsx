import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CloudOutlined,
  DeleteOutlined,
  InboxOutlined,
  LoginOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import {
  deleteICloudAccount,
  deleteICloudAlias,
  generateICloudAliases,
  importICloudCookie,
  listICloudAccounts,
  listICloudAliasMessages,
  listICloudAliases,
  setICloudAccountEnabled,
  syncICloudAccount,
  type ICloudAccount,
  type ICloudAlias,
  type ICloudMessage,
} from '@/api/icloud'
import { ICloudLoginModal } from '@/components/icloud/ICloudLoginModal'
import {
  DEFAULT_ICLOUD_IMAP_HOST,
  DEFAULT_ICLOUD_IMAP_PORT,
  ICLOUD_HOURLY_ALIAS_LIMIT,
  ICLOUD_REGION_OPTIONS,
  formatDateTime,
  getICloudRegionLabel,
} from '@/lib/icloud'

const { Text, Paragraph } = Typography

export default function ICloudPage() {
  const { message } = App.useApp()
  const [accounts, setAccounts] = useState<ICloudAccount[]>([])
  const [aliases, setAliases] = useState<ICloudAlias[]>([])
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [cookieOpen, setCookieOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [filterAccountId, setFilterAccountId] = useState<number | undefined>()
  const [inboxAlias, setInboxAlias] = useState<ICloudAlias | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [nextAccounts, nextAliases] = await Promise.all([
        listICloudAccounts(),
        listICloudAliases(filterAccountId),
      ])
      setAccounts(nextAccounts)
      setAliases(nextAliases)
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setLoading(false)
    }
  }, [filterAccountId, message])

  useEffect(() => {
    load()
  }, [load])

  const withBusy = async (id: number, action: () => Promise<unknown>, successText: string) => {
    setBusyId(id)
    try {
      await action()
      message.success(successText)
      await load()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  const accountOptions = useMemo(
    () => accounts.map((account) => ({ value: account.id, label: account.email })),
    [accounts],
  )

  const accountColumns = [
    {
      title: '主号',
      dataIndex: 'email',
      render: (email: string, account: ICloudAccount) => (
        <Space direction="vertical" size={0}>
          <Text strong>{email}</Text>
          {account.display_name && <Text type="secondary">{account.display_name}</Text>}
        </Space>
      ),
    },
    {
      title: '区域',
      dataIndex: 'region',
      width: 180,
      render: (region: string) => getICloudRegionLabel(region),
    },
    {
      title: '会话状态',
      width: 200,
      render: (_: unknown, account: ICloudAccount) => (
        <Space size={4} wrap>
          <Tag color={account.credential_state.has_session_cookies ? 'green' : 'red'}>
            Web Session
          </Tag>
          <Tag color={account.credential_state.has_imap_credentials ? 'green' : 'orange'}>IMAP</Tag>
        </Space>
      ),
    },
    {
      title: '隐私邮箱',
      width: 150,
      render: (_: unknown, account: ICloudAccount) => (
        <Tooltip title={`本小时剩余额度 ${account.quota.remaining}/${account.quota.limit}`}>
          <Badge
            count={account.alias_count}
            showZero
            color="#0ea5e9"
            style={{ marginRight: 8 }}
          />
          <Text type="secondary">
            {account.quota.remaining}/{account.quota.limit}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '最近同步',
      dataIndex: 'last_sync_at',
      width: 180,
      render: (value: string | null, account: ICloudAccount) =>
        account.sync_error ? (
          <Tooltip title={account.sync_error}>
            <Tag color="red">同步失败</Tag>
          </Tooltip>
        ) : (
          formatDateTime(value)
        ),
    },
    {
      title: '启用',
      width: 90,
      render: (_: unknown, account: ICloudAccount) => (
        <Switch
          checked={account.enabled}
          loading={busyId === account.id}
          onChange={(enabled) =>
            withBusy(
              account.id,
              () => setICloudAccountEnabled(account.id, enabled),
              enabled ? '已启用' : '已停用',
            )
          }
        />
      ),
    },
    {
      title: '操作',
      width: 180,
      render: (_: unknown, account: ICloudAccount) => (
        <Space>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={busyId === account.id}
            onClick={() =>
              withBusy(account.id, () => syncICloudAccount(account.id), '已从 iCloud 同步隐私邮箱')
            }
          >
            同步
          </Button>
          <Popconfirm
            title="删除主号"
            description="将同时移除本地记录的隐私邮箱，不会删除 iCloud 上游地址。"
            onConfirm={() => withBusy(account.id, () => deleteICloudAccount(account.id), '主号已删除')}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const aliasColumns = [
    {
      title: '隐私邮箱',
      dataIndex: 'address',
      render: (address: string) => <Text copyable>{address}</Text>,
    },
    { title: '标签', dataIndex: 'label', width: 160, render: (value: string) => value || '-' },
    { title: '所属主号', dataIndex: 'account_email', width: 220 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status: string) => (
        <Tag color={status === 'active' ? 'green' : 'default'}>
          {status === 'active' ? '启用' : '已停用'}
        </Tag>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: '操作',
      width: 180,
      render: (_: unknown, alias: ICloudAlias) => (
        <Space>
          <Button size="small" icon={<InboxOutlined />} onClick={() => setInboxAlias(alias)}>
            收件
          </Button>
          <Popconfirm
            title="删除隐私邮箱"
            description="会先在 iCloud 停用并删除该地址，删除后无法恢复。"
            onConfirm={() => withBusy(alias.id, () => deleteICloudAlias(alias.id), '隐私邮箱已删除')}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <Space>
          <CloudOutlined style={{ fontSize: 20 }} />
          <Text strong style={{ fontSize: 18 }}>
            iCloud 隐私邮箱
          </Text>
        </Space>
        <Space>
          <Button icon={<LoginOutlined />} type="primary" onClick={() => setLoginOpen(true)}>
            应用内登录
          </Button>
          <Button onClick={() => setCookieOpen(true)}>手工导入 Cookie</Button>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} />
        </Space>
      </div>

      <Tabs
        defaultActiveKey="accounts"
        items={[
          {
            key: 'accounts',
            label: `主号管理 (${accounts.length})`,
            children: (
              <Card>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={`收件走 iCloud IMAP TLS，Web Session 只用于 Hide My Email 管理；Apple 限制每个主号每滚动小时最多生成 ${ICLOUD_HOURLY_ALIAS_LIMIT} 个隐私邮箱。`}
                />
                <Table
                  rowKey="id"
                  loading={loading}
                  columns={accountColumns}
                  dataSource={accounts}
                  pagination={false}
                  locale={{
                    emptyText: (
                      <Empty description="还没有 iCloud 主号，点击右上角完成 Apple ID 登录" />
                    ),
                  }}
                />
              </Card>
            ),
          },
          {
            key: 'aliases',
            label: `隐私邮箱 (${aliases.length})`,
            children: (
              <Card>
                <Space style={{ marginBottom: 16 }}>
                  <Select
                    allowClear
                    placeholder="全部主号"
                    style={{ width: 260 }}
                    value={filterAccountId}
                    onChange={setFilterAccountId}
                    options={accountOptions}
                  />
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    disabled={accounts.length === 0}
                    onClick={() => setGenerateOpen(true)}
                  >
                    生成隐私邮箱
                  </Button>
                </Space>
                <Table
                  rowKey="id"
                  loading={loading}
                  columns={aliasColumns}
                  dataSource={aliases}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  locale={{
                    emptyText: (
                      <Empty
                        description={
                          accounts.length === 0
                            ? '请先添加 iCloud 主号'
                            : '还没有隐私邮箱，点击“生成隐私邮箱”开始'
                        }
                      />
                    ),
                  }}
                />
              </Card>
            ),
          },
        ]}
      />

      <ICloudLoginModal
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        onCompleted={() => load()}
      />

      <CookieImportModal
        open={cookieOpen}
        onClose={() => setCookieOpen(false)}
        onImported={() => load()}
      />

      <GenerateAliasModal
        open={generateOpen}
        accounts={accounts}
        defaultAccountId={filterAccountId ?? accounts[0]?.id}
        onClose={() => setGenerateOpen(false)}
        onGenerated={() => load()}
      />

      <AliasInboxDrawer alias={inboxAlias} onClose={() => setInboxAlias(null)} />
    </div>
  )
}

function CookieImportModal({
  open,
  onClose,
  onImported,
}: {
  open: boolean
  onClose: () => void
  onImported: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      const account = await importICloudCookie(values)
      message.success(`已导入主号 ${account.email}`)
      onImported()
      onClose()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="手工导入 iCloud Cookie"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="校验并导入"
      width={620}
      destroyOnHidden
    >
      <Paragraph type="secondary">
        在浏览器登录 iCloud 后，从 <Text code>setup/ws/1/validate</Text> 请求复制完整 Cookie。
        应用内登录不可用时才需要这种方式。
      </Paragraph>
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          region: 'global',
          imap_host: DEFAULT_ICLOUD_IMAP_HOST,
          imap_port: DEFAULT_ICLOUD_IMAP_PORT,
        }}
      >
        <Form.Item
          name="cookie_header"
          label="Cookie"
          rules={[{ required: true, message: '请粘贴 Cookie' }]}
        >
          <Input.TextArea rows={5} placeholder="X-APPLE-WEBAUTH-USER=...; X-APPLE-WEBAUTH-TOKEN=..." />
        </Form.Item>
        <Form.Item name="email" label="Apple ID（可选）" extra="留空则从 iCloud 返回的账号信息中读取">
          <Input placeholder="owner@icloud.com" />
        </Form.Item>
        <Form.Item name="region" label="账号区域">
          <Select options={ICLOUD_REGION_OPTIONS} />
        </Form.Item>
        <Form.Item name="imap_password" label="IMAP 应用专用密码（可选）">
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function GenerateAliasModal({
  open,
  accounts,
  defaultAccountId,
  onClose,
  onGenerated,
}: {
  open: boolean
  accounts: ICloudAccount[]
  defaultAccountId?: number
  onClose: () => void
  onGenerated: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) form.setFieldsValue({ account_id: defaultAccountId, count: 1 })
  }, [open, defaultAccountId, form])

  const submit = async () => {
    const values = await form.validateFields()
    setBusy(true)
    try {
      const created = await generateICloudAliases(values)
      message.success(`已生成 ${created.length} 个隐私邮箱`)
      onGenerated()
      onClose()
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const remaining = accounts.find((item) => item.id === form.getFieldValue('account_id'))?.quota
    .remaining

  return (
    <Modal
      open={open}
      title="生成隐私邮箱"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="生成"
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item name="account_id" label="主号" rules={[{ required: true, message: '请选择主号' }]}>
          <Select options={accounts.map((item) => ({ value: item.id, label: item.email }))} />
        </Form.Item>
        <Form.Item
          name="count"
          label="生成数量"
          extra={
            remaining === undefined
              ? `Apple 限制每个主号每滚动小时最多 ${ICLOUD_HOURLY_ALIAS_LIMIT} 个`
              : `该主号本小时剩余额度 ${remaining} 个`
          }
        >
          <InputNumber min={1} max={ICLOUD_HOURLY_ALIAS_LIMIT} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="label" label="标签（可选）">
          <Input placeholder="any-auto-register" />
        </Form.Item>
        <Form.Item name="note" label="备注（可选）">
          <Input placeholder="批量生成" />
        </Form.Item>
      </Form>
    </Modal>
  )
}

function AliasInboxDrawer({ alias, onClose }: { alias: ICloudAlias | null; onClose: () => void }) {
  const { message } = App.useApp()
  const [messages, setMessages] = useState<ICloudMessage[]>([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!alias) return
    setLoading(true)
    try {
      setMessages(await listICloudAliasMessages(alias.id))
    } catch (error) {
      message.error((error as Error).message)
      setMessages([])
    } finally {
      setLoading(false)
    }
  }, [alias, message])

  useEffect(() => {
    setMessages([])
    load()
  }, [load])

  return (
    <Drawer
      open={Boolean(alias)}
      onClose={onClose}
      width={640}
      title={alias ? `收件箱 · ${alias.address}` : '收件箱'}
      extra={<Button icon={<ReloadOutlined spin={loading} />} onClick={load} />}
    >
      <Paragraph type="secondary">
        每次打开都会通过 IMAP 实时读取主号收件箱中投递到该地址的邮件，不使用本地缓存。
      </Paragraph>
      <List
        loading={loading}
        dataSource={messages}
        locale={{ emptyText: <Empty description="暂时没有收到邮件" /> }}
        renderItem={(item) => (
          <List.Item>
            <List.Item.Meta
              title={
                <Space>
                  <Text strong>{item.subject || '(无主题)'}</Text>
                  {!item.is_read && <Tag color="blue">未读</Tag>}
                </Space>
              }
              description={
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Text type="secondary">
                    {item.from.name ? `${item.from.name} <${item.from.email}>` : item.from.email}
                    {' · '}
                    {formatDateTime(item.received_at)}
                  </Text>
                  <Text>{item.snippet}</Text>
                </Space>
              }
            />
          </List.Item>
        )}
      />
    </Drawer>
  )
}
