"""支付渠道注册表（Registry + Strategy）。"""

from __future__ import annotations

from typing import Any

from .contracts import PaymentChannel


class PaymentChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, PaymentChannel] = {}

    def register(self, channel: PaymentChannel) -> PaymentChannel:
        key = str(channel.name).strip().lower()
        if not key:
            raise ValueError("支付渠道必须提供 name")
        self._channels[key] = channel
        return channel

    def get(self, name: str) -> PaymentChannel:
        key = str(name or "").strip().lower()
        try:
            return self._channels[key]
        except KeyError as exc:
            raise KeyError(f"支付渠道 '{name}' 未注册，可用: {sorted(self._channels)}") from exc

    def list(self) -> list[dict[str, str]]:
        return [
            {
                "name": key,
                "display_name": channel.display_name,
                "operations": list(getattr(channel, "operations", ["link", "pay"])),
                "option_schema": dict(getattr(channel, "option_schema", {})),
            }
            for key, channel in sorted(self._channels.items())
        ]


payment_channels = PaymentChannelRegistry()


def register_payment_channel(channel: PaymentChannel) -> PaymentChannel:
    return payment_channels.register(channel)


def load_builtin_payment_channels() -> None:
    """延迟加载内置渠道，避免导入 API 时初始化卡片或网络客户端。"""
    from platforms.chatgpt.payment_channels.direct import direct_card_channel

    payment_channels.register(direct_card_channel)
