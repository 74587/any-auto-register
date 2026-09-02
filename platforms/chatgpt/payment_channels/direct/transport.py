"""直卡渠道的 HTTP 传输层。

业务策略不直接依赖 curl_cffi，后续渠道可以替换传输实现或注入测试客户端。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from core.proxy_utils import build_requests_proxy_config

try:
    from curl_cffi import requests as _curl
except Exception:  # pragma: no cover
    _curl = None


APP_BASE = "https://chatgpt.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.7423.118 Safari/537.36"
)


def make_session(proxy: str = ""):
    if _curl is None:
        raise RuntimeError("直卡渠道需要 curl_cffi")
    session = _curl.Session(impersonate="chrome146")
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    if proxy:
        session.proxies = build_requests_proxy_config(proxy) or {}
    return session


def _req(session, method: str, url: str, **kwargs):
    verify = kwargs.pop("verify", True)
    attempts = [verify, False] if verify and url.startswith("https://") else [verify]
    last: Exception | None = None
    for current_verify in attempts:
        try:
            response = session.request(method, url, verify=current_verify, **kwargs)
            try:
                from core.traffic import record_response

                record_response(response)
            except Exception:
                pass
            return response
        except Exception as exc:
            last = exc
            if current_verify and "ssl" not in f"{type(exc).__name__}:{exc}".lower():
                raise
    raise last or RuntimeError("request_failed")


def parse_cookie_header(raw: str = "") -> dict[str, str]:
    """Parse a Cookie header while keeping the last value for duplicate names."""
    cookies: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and value:
            cookies[name.strip()] = value.strip()
    return cookies


def cookie_header(
    device_id: str,
    session_token: str = "",
    cookie_jar: Mapping[str, str] | None = None,
) -> str:
    """Build the browser Cookie header used across checkout stages."""
    cookies = dict(cookie_jar or {})
    cookies["oai-did"] = str(device_id or cookies.get("oai-did") or uuid.uuid4())
    if session_token:
        cookies["__Secure-next-auth.session-token"] = session_token
        cookies["next-auth.session-token"] = session_token
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if value)


def merge_session_cookies(cookie_jar: dict[str, str], session: Any) -> None:
    """Persist Set-Cookie values so a later stage can reuse the same context."""
    try:
        for cookie in session.cookies:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            if name and value:
                cookie_jar[name] = value
    except Exception:
        return


def chatgpt_session(
    proxy: str,
    access_token: str,
    session_token: str = "",
    *,
    device_id: str = "",
    cookie_jar: Mapping[str, str] | None = None,
):
    session = make_session(proxy)
    device_id = str(device_id or (cookie_jar or {}).get("oai-did") or uuid.uuid4())
    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/",
            "oai-device-id": device_id,
            "oai-language": "en-US",
            "Cookie": cookie_header(device_id, session_token, cookie_jar),
        }
    )
    return session


def json_body(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        data = {"raw": (getattr(response, "text", "") or "")[:800]}
    return data if isinstance(data, dict) else {}
