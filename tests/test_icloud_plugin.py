"""iCloud 插件：注册即生成隐私邮箱，以及账号有效性与平台动作。"""

import pytest

from core.base_platform import Account, AccountStatus, RegisterConfig
from platforms.icloud.models import PrivateEmail
from platforms.icloud.plugin import ICloudPlatform


class _StubService:
    HOURLY_ALIAS_LIMIT = 5

    def __init__(self):
        self.generated = []
        self.deleted = []

    class _Row:
        id = 7
        email = "owner@icloud.com"
        region = "global"

    def resolve_account(self, _email=""):
        return self._Row()

    def get_account(self, _account_id):
        return self._Row()

    def load_credentials(self, _row):
        return object()

    def alias_quota(self, _account_id):
        return {"limit": 5, "used": 1, "remaining": 4, "reset_at": None}

    def generate_alias(self, account_id, *, label="", note="", proxy=None):
        self.generated.append((account_id, label, note, proxy))
        return {
            "id": 11,
            "address": "alias@icloud.com",
            "provider_id": "anon-1",
            "label": label,
            "note": note,
            "status": "active",
        }

    def delete_alias(self, alias_id):
        self.deleted.append(alias_id)

    def fetch_account_messages(self, _account_id, *, limit=20, recipient=""):
        return []


@pytest.fixture
def service(monkeypatch):
    import services.icloud_service as real_service

    stub = _StubService()
    for name in (
        "resolve_account",
        "get_account",
        "load_credentials",
        "alias_quota",
        "generate_alias",
        "delete_alias",
        "fetch_account_messages",
    ):
        monkeypatch.setattr(real_service, name, getattr(stub, name))
    return stub


def test_register_generates_alias_and_records_owner(service):
    platform = ICloudPlatform(
        RegisterConfig(
            proxy="http://127.0.0.1:1080",
            extra={"icloud_alias_label": "批量", "icloud_alias_note": "备注"},
        )
    )
    logs: list[str] = []
    platform._log_fn = logs.append

    account = platform.register()

    assert service.generated == [(7, "批量", "备注", "http://127.0.0.1:1080")]
    assert account.platform == "icloud"
    assert account.email == "alias@icloud.com"
    assert account.password == ""
    assert account.status is AccountStatus.REGISTERED
    assert account.extra["icloud_account_id"] == 7
    assert account.extra["alias_id"] == 11
    assert any("剩余额度 4/5" in line for line in logs)


def test_register_does_not_consume_a_mailbox():
    """iCloud 自带隐私邮箱，任务运行时传进来的邮箱池必须被忽略。"""
    sentinel = object()

    platform = ICloudPlatform(RegisterConfig(), mailbox=sentinel)

    assert platform.mailbox is None


def test_check_valid_requires_the_alias_to_still_be_active(monkeypatch, service):
    from contextlib import contextmanager

    import platforms.icloud.client as client_module

    class _Client:
        def list_private_emails(self, _credentials):
            return [
                PrivateEmail(address="alias@icloud.com", status="active"),
                PrivateEmail(address="retired@icloud.com", status="inactive"),
            ]

    @contextmanager
    def _factory(**_kwargs):
        yield _Client()

    monkeypatch.setattr(client_module, "web_client", _factory)
    platform = ICloudPlatform(RegisterConfig())

    def _account(email: str) -> Account:
        return Account(
            platform="icloud",
            email=email,
            password="",
            extra={"icloud_account_id": 7},
        )

    assert platform.check_valid(_account("alias@icloud.com")) is True
    assert platform.check_valid(_account("retired@icloud.com")) is False
    assert platform.check_valid(_account("unknown@icloud.com")) is False


def test_delete_alias_action_needs_a_local_record(service):
    platform = ICloudPlatform(RegisterConfig())
    account = Account(platform="icloud", email="alias@icloud.com", password="")

    assert platform.execute_action("delete_alias", account, {})["ok"] is False

    account.extra = {"alias_id": 11, "icloud_account_id": 7}
    assert platform.execute_action("delete_alias", account, {})["ok"] is True
    assert service.deleted == [11]
