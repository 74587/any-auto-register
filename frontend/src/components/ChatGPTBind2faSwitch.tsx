import { Space, Switch, Tag, Typography } from 'antd'

const { Text } = Typography

type ChatGPTBind2faSwitchProps = {
  enabled: boolean
  onChange: (enabled: boolean) => void
}

export function ChatGPTBind2faSwitch({
  enabled,
  onChange,
}: ChatGPTBind2faSwitchProps) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space align="center" wrap>
        <Switch
          checked={enabled}
          checkedChildren="绑定"
          unCheckedChildren="不绑"
          onChange={onChange}
        />
        <Tag color={enabled ? 'processing' : 'default'}>
          {enabled ? '注册后自动绑' : '默认关闭'}
        </Tag>
      </Space>
      <Text type="secondary">
        {enabled
          ? '注册成功后顺手给账号绑一个 TOTP 双因素，密钥会写进账号详情，也会在任务日志里打一遍。'
          : '不绑 2FA，账号登录只需要密码或邮箱验证码。'}
      </Text>
      {enabled && (
        <Text type="warning">
          密钥只在绑定那一刻下发一次，服务端取不回；丢了这个号就再也登不进去。
        </Text>
      )}
    </Space>
  )
}
