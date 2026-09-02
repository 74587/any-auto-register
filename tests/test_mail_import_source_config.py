"""邮箱导入面板选中的那一栏要能存住。

以前只有 mail_provider（microsoft / applemail）落库，界面上的 Outlook / Hotmail /
MailAPI URL 三个视图靠反推，反推不出 MailAPI URL——选完退出再进来就变回 Outlook。
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from services.mail_imports import (
    align_source_with_provider,
    normalize_mail_import_source,
    resolve_mail_provider_from_source,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.config_store as config_store_module
    import core.db as db

    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(config_store_module, "engine", engine)

    from main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("stored", "provider", "expected"),
    [
        ("mailapi", "microsoft", "mailapi"),
        ("hotmail", "microsoft", "hotmail"),
        ("MailAPI", "microsoft", "mailapi"),
        # 旧库里只有 microsoft/applemail 两个值
        ("microsoft", "microsoft", "outlook"),
        ("", "applemail", "applemail"),
        ("", "microsoft", "outlook"),
        ("", "luckmail", "outlook"),
        ("nonsense", "applemail", "applemail"),
    ],
)
def test_normalize_mail_import_source(stored, provider, expected):
    assert normalize_mail_import_source(stored, provider) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("outlook", "microsoft"),
        ("hotmail", "microsoft"),
        ("mailapi", "microsoft"),
        ("applemail", "applemail"),
    ],
)
def test_three_microsoft_views_share_one_pool(source, expected):
    assert resolve_mail_provider_from_source(source) == expected


def test_provider_wins_when_the_two_disagree():
    assert align_source_with_provider("mailapi", "applemail") == "applemail"
    assert align_source_with_provider("applemail", "microsoft") == "outlook"
    assert align_source_with_provider("mailapi", "microsoft") == "mailapi"
    # provider 不是邮箱导入时视图照存，等用户切回来还是他选的那一栏
    assert align_source_with_provider("mailapi", "luckmail") == "mailapi"


def test_mailapi_view_survives_a_reload(client):
    client.put(
        "/api/config",
        json={"data": {"mail_provider": "microsoft", "mail_import_source": "mailapi"}},
    )

    config = client.get("/api/config").json()

    assert config["mail_import_source"] == "mailapi"
    assert config["mail_provider"] == "microsoft"


def test_panel_can_change_only_the_view(client):
    client.put("/api/config", json={"data": {"mail_provider": "applemail"}})

    client.put("/api/config", json={"data": {"mail_import_source": "mailapi"}})

    assert client.get("/api/config").json()["mail_import_source"] == "mailapi"


def test_config_without_a_stored_view_falls_back_instead_of_returning_blank(client):
    client.put("/api/config", json={"data": {"mail_provider": "applemail"}})

    assert client.get("/api/config").json()["mail_import_source"] == "applemail"


def test_payment_proxy_survives_a_reload(client):
    proxy = "socks5h://user:pass@example.test:1080"
    response = client.put("/api/config", json={"data": {"payment_proxy": proxy}})

    assert response.status_code == 200
    assert client.get("/api/config").json()["payment_proxy"] == proxy


def test_payment_operation_proxies_survive_a_reload(client):
    link_proxy = "socks5h://link.example.test:1080"
    pay_proxy = "socks5h://pay.example.test:1080"
    response = client.put("/api/config", json={"data": {
        "payment_link_proxy": link_proxy,
        "payment_pay_proxy": pay_proxy,
    }})

    assert response.status_code == 200
    config = client.get("/api/config").json()
    assert config["payment_link_proxy"] == link_proxy
    assert config["payment_pay_proxy"] == pay_proxy
