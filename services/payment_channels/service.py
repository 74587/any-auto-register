"""支付渠道门面：把 ORM 账号转换成不可变快照后调用策略。"""

from __future__ import annotations

from typing import Any, Mapping

from core.db import AccountModel

from .contracts import PaymentAccount, PaymentResult
from .registry import load_builtin_payment_channels, payment_channels


def account_context(account: AccountModel) -> PaymentAccount:
    extra = account.get_extra()
    return PaymentAccount(
        platform=account.platform,
        account_id=str(account.id or ""),
        email=account.email,
        access_token=str(extra.get("access_token") or account.token or ""),
        session_token=str(extra.get("session_token") or ""),
        user_id=str(account.user_id or ""),
        cookies=str(extra.get("cookies") or ""),
    )


def _channel(name: str):
    load_builtin_payment_channels()
    return payment_channels.get(name)


def create_link(
    account: AccountModel,
    channel: str,
    *,
    options: Mapping[str, Any] | None = None,
) -> PaymentResult:
    return _channel(channel).create_link(account_context(account), options=options)


def create_link_for_context(
    account: PaymentAccount,
    channel: str,
    *,
    options: Mapping[str, Any] | None = None,
) -> PaymentResult:
    return _channel(channel).create_link(account, options=options)


def pay(
    account: AccountModel,
    channel: str,
    *,
    options: Mapping[str, Any] | None = None,
) -> PaymentResult:
    return _channel(channel).pay(account_context(account), options=options)


def pay_for_context(
    account: PaymentAccount,
    channel: str,
    *,
    options: Mapping[str, Any] | None = None,
) -> PaymentResult:
    return _channel(channel).pay(account, options=options)
