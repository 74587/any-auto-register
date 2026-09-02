# 支付渠道架构

支付渠道采用 Registry + Strategy：

- `services/payment_channels/contracts.py` 定义 `PaymentAccount`、`PaymentResult` 和 `PaymentChannel` 契约。
- `services/payment_channels/registry.py` 管理渠道注册；内置 `direct` 由 ChatGPT 直卡策略提供。
- `services/payment_channels/service.py` 将 `AccountModel` 转成不可变账号快照，再调度渠道，网络流程不持有数据库会话。
- `platforms/chatgpt/payment_channels/direct/` 包含直卡 checkout/update、Stripe SetupIntent 绑卡、订阅确认、卡片库和免税地址。

## API

- `GET /api/payments/channels`
- `POST /api/payments/{account_id}/link`
- `POST /api/payments/{account_id}/pay`

请求体：

```json
{
  "channel": "direct",
  "options": {
    "country": "PH",
    "currency": "PHP",
    "card_id": 1,
    "taxfree_state": "DE",
    "link_proxy": "socks5h://link-proxy.example:1080",
    "pay_proxy": "socks5h://pay-proxy.example:1080"
  }
}
```

直卡的提链和支付使用独立代理：`link_proxy` 仅用于 checkout 链接流程，`pay_proxy` 仅用于绑卡和扣款。两者为空时分别读取后端配置 `payment_link_proxy`、`payment_pay_proxy`，再回退到兼容配置 `payment_proxy` 或代理池。

新增渠道时，实现 `PaymentChannel` 的 `create_link` 和 `pay`，然后在
`load_builtin_payment_channels()` 注册，不需要修改账号模型或 API 路由。
