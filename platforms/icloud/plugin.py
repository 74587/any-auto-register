"""iCloud 隐私邮箱平台插件。

这里的“注册”指从已登录的 iCloud 主号批量生成 Hide My Email 地址：
不需要外部临时邮箱，也不需要验证码，全程走 Apple Web 协议。
"""

from __future__ import annotations

from typing import Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register

from .constants import ALIAS_STATUS_ACTIVE
from .errors import ICloudError


@register
class ICloudPlatform(BasePlatform):
    name = "icloud"
    display_name = "iCloud 隐私邮箱"
    version = "1.0.0"
    supported_executors = ["protocol"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        # iCloud 自带隐私邮箱，注册流程不消耗外部邮箱池。
        self.mailbox = None

    def register(self, email: str = None, password: str = None) -> Account:
        from services import icloud_service

        extra = (self.config.extra or {}) if self.config else {}
        log = getattr(self, "_log_fn", print)

        account_row = icloud_service.resolve_account(str(extra.get("icloud_account_email") or ""))
        quota = icloud_service.alias_quota(account_row.id)
        log(
            f"  [iCloud] 主号 {account_row.email} 本小时剩余额度 "
            f"{quota['remaining']}/{quota['limit']}"
        )

        alias = icloud_service.generate_alias(
            account_row.id,
            label=str(extra.get("icloud_alias_label") or "").strip(),
            note=str(extra.get("icloud_alias_note") or "").strip(),
            proxy=self.config.proxy if self.config else None,
        )
        log(f"  [iCloud] 已生成隐私邮箱 {alias['address']}")

        return Account(
            platform=self.name,
            email=alias["address"],
            password="",
            user_id=str(alias.get("provider_id") or ""),
            region=account_row.region,
            status=AccountStatus.REGISTERED,
            extra={
                "icloud_account_id": account_row.id,
                "icloud_account_email": account_row.email,
                "alias_id": alias.get("id"),
                "label": alias.get("label", ""),
                "note": alias.get("note", ""),
                "status": alias.get("status", ALIAS_STATUS_ACTIVE),
            },
        )

    def check_valid(self, account: Account) -> bool:
        """隐私邮箱可用等价于它仍在主号的上游列表里且处于启用状态。"""
        from services import icloud_service

        account_id = self._owner_account_id(account)
        if account_id is None:
            return False
        try:
            credentials = icloud_service.load_credentials(icloud_service.get_account(account_id))
            from .client import web_client

            with web_client(proxy=self.config.proxy if self.config else None) as client:
                private_emails = client.list_private_emails(credentials)
        except ICloudError:
            return False
        target = str(account.email or "").strip().lower()
        return any(
            item.address == target and item.status == ALIAS_STATUS_ACTIVE
            for item in private_emails
        )

    def get_platform_actions(self) -> list:
        return [
            {"id": "fetch_inbox", "label": "实时收件", "params": []},
            {"id": "delete_alias", "label": "删除隐私邮箱", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        from services import icloud_service

        alias_id = (account.extra or {}).get("alias_id")
        if action_id == "fetch_inbox":
            account_id = self._owner_account_id(account)
            if account_id is None:
                return {"ok": False, "error": "该隐私邮箱缺少所属主号信息"}
            messages = icloud_service.fetch_account_messages(
                account_id,
                limit=int(params.get("limit") or 20),
                recipient=account.email,
            )
            return {"ok": True, "data": [message.to_dict() for message in messages]}
        if action_id == "delete_alias":
            if not alias_id:
                return {"ok": False, "error": "该隐私邮箱缺少本地记录，无法删除"}
            icloud_service.delete_alias(int(alias_id))
            return {"ok": True, "data": {"alias_id": alias_id}}
        return super().execute_action(action_id, account, params)

    @staticmethod
    def _owner_account_id(account: Account) -> Optional[int]:
        from services import icloud_service

        extra = account.extra or {}
        if extra.get("icloud_account_id"):
            return int(extra["icloud_account_id"])
        owner = icloud_service.find_account_by_email(str(extra.get("icloud_account_email") or ""))
        return owner.id if owner else None
