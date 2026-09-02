import base64
import json
from unittest import mock

from core import traffic
from core.db import AccountModel
from services.payment_channels.contracts import PaymentAccount
from services.payment_channels.registry import load_builtin_payment_channels, payment_channels
from services.payment_channels.service import account_context, create_link_for_context
from platforms.chatgpt.payment_channels.direct.channel import DirectCardChannel, _proxy
from platforms.chatgpt.payment_channels.direct.card_store import CardStore
from platforms.chatgpt.payment_channels.direct import transport
from api.payments import (
    _configured_payment_proxy,
    _payment_request_options,
    _public_card,
    list_channels,
)


def _token(account_id="chatgpt-account"):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_payment_registry_loads_direct_channel():
    load_builtin_payment_channels()
    assert payment_channels.get("direct").display_name == "直卡"


def test_direct_payment_traffic_context_is_available():
    traffic.set_block(traffic.BLOCK_PAY)
    try:
        assert traffic.get_block() == traffic.BLOCK_PAY
    finally:
        traffic.clear_block()
    assert traffic.get_block() == traffic.BLOCK_CHAIN


def test_builtin_test_card_is_disabled_and_cleaned_by_default(tmp_path):
    db_path = str(tmp_path / "cards.db")
    enabled = CardStore(db_path, enable_builtin_test_card=True)
    assert enabled.list_cards()[0]["source"] == "builtin_test"

    disabled = CardStore(db_path, enable_builtin_test_card=False)
    assert disabled.list_cards() == []


def test_direct_channel_uses_saved_payment_proxy():
    def get(key, default=""):
        return {
            "payment_link_proxy": "socks5h://link-saved:1080",
            "payment_pay_proxy": "socks5h://pay-saved:1080",
        }.get(key, default)

    with mock.patch("core.config_store.config_store.get", side_effect=get):
        assert _proxy({}, "link") == "socks5h://link-saved:1080"
        assert _proxy({}, "pay") == "socks5h://pay-saved:1080"
        assert _proxy({"link_proxy": "http://task-link:8080"}, "link") == "http://task-link:8080"
        assert _proxy({"pay_proxy": "http://task-pay:8080"}, "pay") == "http://task-pay:8080"


def test_payment_task_uses_operation_proxy_for_scheduler(monkeypatch):
    monkeypatch.setattr(
        "api.payments.config_store.get",
        lambda key, default="": {"payment_link_proxy": "socks5h://ph:1080"}.get(key, default),
    )
    assert _configured_payment_proxy("link", {}) == "socks5h://ph:1080"


def test_scheduler_proxy_is_forwarded_to_the_matching_channel_field():
    link_options = _payment_request_options("link", {}, "socks5h://ph:1080")
    pay_options = _payment_request_options("pay", {}, "socks5h://us:1080")

    assert link_options["link_proxy"] == "socks5h://ph:1080"
    assert pay_options["pay_proxy"] == "socks5h://us:1080"


def test_account_context_is_detached_from_orm():
    account = AccountModel(platform="chatgpt", email="a@example.com", token="at")
    account.set_extra({"session_token": "st", "access_token": "at2"})
    context = account_context(account)
    assert isinstance(context, PaymentAccount)
    assert context.account_id == str(account.id or "")
    assert context.access_token == "at2"
    assert context.session_token == "st"


def test_direct_link_strategy_runs_checkout_then_zero_update():
    strategy = DirectCardChannel()
    account = PaymentAccount("chatgpt", "db-1", "a@example.com", _token())
    with mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._proxy", return_value=""
    ), mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._checkout",
        return_value={
            "status": 200,
            "ok": True,
            "checkout_session_id": "cs_test",
            "processor_entity": "openai_llc",
            "body": {},
        },
    ) as checkout, mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._update_zero",
        return_value={"status": 200, "ok": True, "body": {"amount_due": 0}},
    ) as update, mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._probe_hosted_page",
        return_value={"status": 200, "ok": True, "application_error": False},
    ):
        result = strategy.create_link(account, options={"country": "PH", "currency": "PHP"})
    assert result.ok
    assert result.data["link"].endswith("/checkout/openai_llc/cs_test")
    checkout.assert_called_once()
    update.assert_called_once()


def test_direct_link_does_not_require_zero_amount_detection():
    strategy = DirectCardChannel()
    account = PaymentAccount("chatgpt", "db-1", "a@example.com", _token())
    with mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._proxy", return_value=""
    ), mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._checkout",
        return_value={
            "status": 200,
            "ok": True,
            "checkout_session_id": "oaics_test",
            "processor_entity": "openai_llc",
            "body": {},
        },
    ), mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._update_zero",
        return_value={"status": 200, "ok": True, "body": {}},
    ), mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel._probe_hosted_page",
        return_value={"status": 200, "ok": True, "application_error": False},
    ):
        result = strategy.create_link(account)
    assert result.ok
    assert result.data["link"].endswith("/checkout/openai_llc/oaics_test")


def test_chatgpt_session_keeps_device_and_imported_cookies():
    fake_session = mock.Mock()
    with mock.patch.object(transport, "make_session", return_value=fake_session):
        session = transport.chatgpt_session(
            "",
            "access",
            "session",
            device_id="device-1",
            cookie_jar={"oai-sc": "sc-1"},
        )
    assert session is fake_session
    headers = fake_session.headers.update.call_args.args[0]
    assert headers["oai-device-id"] == "device-1"
    assert "oai-did=device-1" in headers["Cookie"]
    assert "oai-sc=sc-1" in headers["Cookie"]
    assert transport.USER_AGENT.endswith("Chrome/146.0.7423.118 Safari/537.36")


def test_direct_pay_delegates_to_channel_flow():
    strategy = DirectCardChannel()
    account = PaymentAccount("chatgpt", "db-1", "a@example.com", _token("uuid-1"))
    card = {"number": "4242424242424242", "exp_month": "12", "exp_year": "30", "cvc": "123"}
    with mock.patch.object(
        strategy,
        "create_link",
        return_value=mock.Mock(ok=True, data={"checkout_session_id": "cs", "processor_entity": "openai_llc"}),
    ), mock.patch(
        "platforms.chatgpt.payment_channels.direct.channel.bind_and_pay",
        return_value={"ok": True, "subscription_plan": "plus"},
    ) as bind:
        result = strategy.pay(account, options={"card": card, "link_proxy": "http://link:8080", "pay_proxy": "http://pay:8080"})
    assert result.ok
    assert result.data["subscription_plan"] == "plus"
    assert bind.call_args.args[2] == "uuid-1"
    assert bind.call_args.args[0] == "http://pay:8080"


def test_service_dispatches_by_channel_name():
    account = PaymentAccount("chatgpt", "db-1", "a@example.com", _token())
    with mock.patch.object(payment_channels, "get", return_value=mock.Mock(create_link=mock.Mock(return_value="ok"))) as get:
        assert create_link_for_context(account, "direct") == "ok"
    get.assert_called_once_with("direct")


def test_channel_metadata_exposes_renderable_operations():
    payload = list_channels()
    direct = next(item for item in payload["channels"] if item["name"] == "direct")
    assert direct["operations"] == ["link", "pay"]
    assert direct["option_schema"]["pay"][0]["control"] == "card"
    assert any(field["key"] == "link_proxy" for field in direct["option_schema"]["link"])
    assert any(field["key"] == "pay_proxy" for field in direct["option_schema"]["pay"])


def test_public_card_never_exposes_full_number_or_cvc():
    public = _public_card({"id": 7, "number": "4242424242424242", "cvc": "123", "uses": 1, "max_uses": 10})
    assert public["last4"] == "4242"
    assert "number" not in public
    assert "cvc" not in public
