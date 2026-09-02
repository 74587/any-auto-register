"""ChatGPT 直卡渠道。

这是一个 Strategy：只关心 ChatGPT 直卡所需的 checkout、绑卡和订阅确认，
不把渠道细节泄漏到统一支付服务或账号 ORM 中。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from services.payment_channels.contracts import PaymentAccount, PaymentResult

from .bind_card import bind_and_pay
from .card_store import card_store
from .taxfree_store import generate_address
from .transport import (
    APP_BASE,
    _req,
    chatgpt_session,
    cookie_header,
    json_body,
    merge_session_cookies,
    parse_cookie_header,
)


@dataclass
class _CheckoutContext:
    device_id: str
    cookie_jar: dict[str, str]


def _checkout_context(account: PaymentAccount) -> _CheckoutContext:
    cookie_jar = parse_cookie_header(account.cookies)
    device_id = str(cookie_jar.get("oai-did") or uuid.uuid4())
    cookie_jar["oai-did"] = device_id
    return _CheckoutContext(device_id, cookie_jar)


def _session(account: PaymentAccount, proxy: str, context: _CheckoutContext):
    session = chatgpt_session(
        proxy,
        account.access_token,
        account.session_token,
        device_id=context.device_id,
        cookie_jar=context.cookie_jar,
    )
    session.headers["Cookie"] = cookie_header(
        context.device_id,
        account.session_token,
        context.cookie_jar,
    )
    return session


def _warmup(session: Any) -> None:
    """Seed the same-session Cloudflare/OAI cookies before creating checkout."""
    try:
        _req(
            session,
            "GET",
            f"{APP_BASE}/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
                "upgrade-insecure-requests": "1",
            },
            timeout=30,
        )
    except Exception:
        pass


def _proxy(options: Mapping[str, Any], kind: str = "link") -> str:
    """Resolve an operation-specific proxy, with legacy compatibility fallback."""
    value = str(options.get(f"{kind}_proxy") or "").strip()
    if value:
        return value
    try:
        from core.config_store import config_store

        value = str(config_store.get(f"payment_{kind}_proxy", "") or "").strip()
        if value:
            return value
        # Existing installations used one shared payment_proxy setting.
        value = str(config_store.get("payment_proxy", "") or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        from core.proxy_pool import proxy_pool

        return str(proxy_pool.get_next("US") or "")
    except Exception:
        return ""


def _chatgpt_account_id(access_token: str) -> str:
    try:
        part = str(access_token).split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        auth = payload.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "").strip()
    except Exception:
        return ""


def _checkout(
    account: PaymentAccount,
    *,
    proxy: str,
    country: str,
    currency: str,
    context: _CheckoutContext,
) -> dict[str, Any]:
    session = _session(account, proxy, context)
    path = "/backend-api/payments/checkout"
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "hosted",
    }
    try:
        _warmup(session)
        merge_session_cookies(context.cookie_jar, session)
        session.headers["Cookie"] = cookie_header(
            context.device_id, account.session_token, context.cookie_jar
        )
        response = _req(
            session,
            "POST",
            f"{APP_BASE}{path}",
            json=payload,
            headers={
                "Referer": f"{APP_BASE}/",
                "x-openai-target-path": path,
                "x-openai-target-route": path,
            },
            timeout=30,
        )
        merge_session_cookies(context.cookie_jar, session)
        body = json_body(response)
        checkout_id = str(body.get("checkout_session_id") or body.get("id") or "").strip()
        entity = str(body.get("processor_entity") or "openai_llc").strip()
        return {
            "status": response.status_code,
            "ok": 200 <= response.status_code < 300 and bool(checkout_id),
            "checkout_session_id": checkout_id,
            "processor_entity": entity,
            "body": body,
        }
    finally:
        session.close()


def _update_zero(
    account: PaymentAccount,
    *,
    proxy: str,
    checkout_id: str,
    entity: str,
    context: _CheckoutContext,
) -> dict[str, Any]:
    session = _session(account, proxy, context)
    path = "/backend-api/payments/checkout/update"
    try:
        response = _req(
            session,
            "POST",
            f"{APP_BASE}{path}",
            json={
                "checkout_session_id": checkout_id,
                "processor_entity": entity,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "promo_campaign": {
                    "promo_campaign_id": "plus-1-month-free",
                    "is_coupon_from_query_param": False,
                },
                "billing_details": {"country": "US", "currency": "USD"},
                "checkout_ui_mode": "hosted",
            },
            headers={
                "Referer": f"{APP_BASE}/checkout/{entity}/{checkout_id}",
                "x-openai-target-path": path,
                "x-openai-target-route": path,
            },
            timeout=30,
        )
        merge_session_cookies(context.cookie_jar, session)
        body = json_body(response)
        return {"status": response.status_code, "ok": response.status_code < 300, "body": body}
    finally:
        session.close()


def _probe_hosted_page(
    account: PaymentAccount,
    *,
    proxy: str,
    checkout_id: str,
    entity: str,
    context: _CheckoutContext,
) -> dict[str, Any]:
    """Detect server errors from the hosted checkout page in the same context."""
    session = _session(account, proxy, context)
    url = f"{APP_BASE}/checkout/{entity}/{checkout_id}"
    try:
        response = _req(
            session,
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            timeout=45,
        )
        merge_session_cookies(context.cookie_jar, session)
        text = str(getattr(response, "text", "") or "")
        application_error = "Application Error" in text
        return {
            "status": response.status_code,
            "ok": response.status_code < 500 and not application_error,
            "application_error": application_error,
        }
    finally:
        session.close()


class DirectCardChannel:
    name = "direct"
    display_name = "直卡"
    operations = ["link", "pay"]
    option_schema = {
        "link": [
            {
                "key": "country",
                "label": "账单国家",
                "control": "select",
                "required": True,
                "default": "PH",
                "options": ["PH", "US", "SG", "TR", "HK", "JP", "GB", "AU", "CA"],
            },
            {
                "key": "currency",
                "label": "币种",
                "control": "select",
                "required": True,
                "default": "PHP",
                "options": ["PHP", "USD", "SGD", "TRY", "HKD", "JPY", "GBP", "AUD", "CAD"],
            },
            {"key": "link_proxy", "label": "提链代理地址", "control": "text", "advanced": True},
        ],
        "pay": [
            {"key": "card_id", "label": "支付卡片", "control": "card", "required": True},
            {
                "key": "taxfree_state",
                "label": "免税州",
                "control": "select",
                "default": "DE",
                "options": ["DE", "NH", "MT", "OR", "AK"],
            },
            {"key": "pay_proxy", "label": "支付代理地址", "control": "text", "advanced": True},
            {"key": "timeout", "label": "超时时间（秒）", "control": "number", "default": 30, "advanced": True},
        ],
    }

    def create_link(
        self, account: PaymentAccount, *, options: Mapping[str, Any] | None = None
    ) -> PaymentResult:
        options = options or {}
        if not account.access_token:
            return PaymentResult.failure(self.name, "create_link", "账号缺少 access_token")
        country = str(options.get("country") or "PH").upper()
        currency = str(options.get("currency") or "PHP").upper()
        proxy = _proxy(options, "link")
        context = _checkout_context(account)
        try:
            checkout = _checkout(
                account,
                proxy=proxy,
                country=country,
                currency=currency,
                context=context,
            )
            if not checkout["ok"]:
                return PaymentResult.failure(
                    self.name,
                    "create_link",
                    f"checkout 失败: status={checkout['status']}",
                    response=checkout["body"],
                )
            updated = _update_zero(
                account,
                proxy=proxy,
                checkout_id=checkout["checkout_session_id"],
                entity=checkout["processor_entity"],
                context=context,
            )
            if not updated["ok"]:
                return PaymentResult.failure(
                    self.name,
                    "create_link",
                    f"update 失败: status={updated['status']}",
                    response=updated["body"],
                )
            page = _probe_hosted_page(
                account,
                proxy=proxy,
                checkout_id=checkout["checkout_session_id"],
                entity=checkout["processor_entity"],
                context=context,
            )
            if not page["ok"]:
                return PaymentResult.failure(
                    self.name,
                    "create_link",
                    f"托管 checkout 页面异常: status={page['status']}",
                    checkout_session_id=checkout["checkout_session_id"],
                )
            link = (
                f"{APP_BASE}/checkout/{checkout['processor_entity']}/"
                f"{checkout['checkout_session_id']}"
            )
            return PaymentResult.success(
                self.name,
                "create_link",
                link=link,
                checkout_session_id=checkout["checkout_session_id"],
                processor_entity=checkout["processor_entity"],
                billing_country=country,
                hosted_status=page["status"],
            )
        except Exception as exc:
            return PaymentResult.failure(self.name, "create_link", f"{type(exc).__name__}: {exc}")

    def pay(
        self, account: PaymentAccount, *, options: Mapping[str, Any] | None = None
    ) -> PaymentResult:
        options = options or {}
        if not account.access_token:
            return PaymentResult.failure(self.name, "pay", "账号缺少 access_token")
        link_proxy = _proxy(options, "link")
        pay_proxy = _proxy(options, "pay")
        link_options = dict(options)
        link_options["link_proxy"] = link_proxy
        card = options.get("card")
        if not isinstance(card, dict):
            card_id = options.get("card_id")
            card = card_store.get_card(int(card_id)) if card_id else card_store.pickup_card()
        if not card:
            return PaymentResult.failure(self.name, "pay", "卡片库无可用卡")
        account_id = str(options.get("chatgpt_account_id") or "").strip()
        if not account_id:
            account_id = _chatgpt_account_id(account.access_token) or account.account_id
        if not account_id:
            return PaymentResult.failure(self.name, "pay", "缺少 chatgpt_account_id")
        try:
            checkout = self.create_link(account, options=link_options)
            if not checkout.ok:
                return checkout
            address = generate_address(str(options.get("taxfree_state") or "DE"), str(card.get("name") or ""))
            billing = {
                "name": card.get("name") or "Test User",
                "line1": address.get("street", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "postal_code": address.get("zip", ""),
                "country": "US",
            }
            result = bind_and_pay(
                pay_proxy,
                account.access_token,
                account_id,
                card,
                account.session_token,
                checkout_id=str(checkout.data["checkout_session_id"]),
                processor=str(checkout.data["processor_entity"]),
                billing=billing,
                currency=str(options.get("currency") or "USD"),
                fast_verify=True,
                timeout=int(options.get("timeout") or 30),
                hcaptcha_token=str(options.get("hcaptcha_token") or ""),
            )
            if not result.get("ok"):
                return PaymentResult.failure(self.name, "pay", str(result.get("error") or "支付失败"), result=result)
            return PaymentResult.success(
                self.name,
                "pay",
                checkout=checkout.data,
                subscription_plan=result.get("subscription_plan", ""),
                card_last4=str(card.get("number", ""))[-4:],
                result=result,
            )
        except Exception as exc:
            return PaymentResult.failure(self.name, "pay", f"{type(exc).__name__}: {exc}")


direct_card_channel = DirectCardChannel()
