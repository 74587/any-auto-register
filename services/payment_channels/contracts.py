"""支付渠道的稳定边界。

渠道实现只接收脱离 ORM 的账号快照，避免网络请求期间占用数据库连接，也让
不同支付渠道可以独立演进。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class PaymentAccount:
    platform: str
    account_id: str
    email: str
    access_token: str
    session_token: str = ""
    user_id: str = ""
    cookies: str = ""


@dataclass
class PaymentResult:
    ok: bool
    channel: str
    operation: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def success(cls, channel: str, operation: str, **data: Any) -> "PaymentResult":
        return cls(True, channel, operation, data=data)

    @classmethod
    def failure(cls, channel: str, operation: str, error: str, **data: Any) -> "PaymentResult":
        return cls(False, channel, operation, data=data, error=error)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "channel": self.channel,
            "operation": self.operation,
            "data": self.data,
            "error": self.error,
        }


class PaymentChannel(Protocol):
    name: str
    display_name: str
    operations: list[str]
    option_schema: Mapping[str, Any]

    def create_link(
        self, account: PaymentAccount, *, options: Mapping[str, Any] | None = None
    ) -> PaymentResult: ...

    def pay(
        self, account: PaymentAccount, *, options: Mapping[str, Any] | None = None
    ) -> PaymentResult: ...
