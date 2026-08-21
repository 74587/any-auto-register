"""给库里已有的 ChatGPT 账号补绑 TOTP 2FA。

和补 RT 一样按代价从低到高试两条路：

    ① 会话复用：拿库里的 session_token/access_token 恢复登录态直接 enroll。
       不发邮件、不碰密码页、不跑 PoW，几秒钟完事。会话还活着的号都走这条。

    ② 协议重登：会话过期时才走。邮箱 + 密码重跑一遍登录正式链再 enroll，
       要一次 PoW，多半还要收一封验证码邮件。

密钥只在 enroll 响应里下发一次，服务端取不回。所以拿到就往 ``extra_json`` 里
写，写不进去这个号的 2FA 等于废了。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from platforms.chatgpt.protocol import AuthFlow, Config
from platforms.chatgpt.protocol.two_factor import (
    TwoFactorBindResult,
    bind_totp_inline,
    bind_totp_via_login,
)
from platforms.chatgpt.protocol_log_relay import mirror_protocol_logs

logger = logging.getLogger(__name__)


def bind_account_two_factor(
    *,
    email: str,
    password: str = "",
    extra: Optional[dict] = None,
    token: str = "",
    config: Optional[dict] = None,
    proxy: Optional[str] = None,
    allow_login: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
    task_control=None,
    attempt_id=None,
) -> TwoFactorBindResult:
    """按账号字段绑 2FA，不落库（落库交给 ``build_extra_patch``）。

    只收纯数据不收 ORM 对象：这一趟要跑几十秒网络请求，调用方得以在这期间把
    数据库连接还回池子里。
    """
    extra = dict(extra or {})
    config = dict(config or _load_config())
    log = log_fn or logger.info

    existing = str(extra.get("totp_secret") or "").strip()
    if existing:
        return TwoFactorBindResult(
            already_bound=True, secret=existing, error_message="库里已经有这个号的 TOTP 密钥"
        )

    session_token = str(extra.get("session_token") or "")
    access_token = str(extra.get("access_token") or token or "")
    device_id = str(extra.get("device_id") or "")

    result = TwoFactorBindResult(error_message="没有可用的绑定路径")
    if session_token or access_token:
        with mirror_protocol_logs(log_fn):
            result = _bind_via_session(
                email=email,
                session_token=session_token,
                access_token=access_token,
                device_id=device_id,
                proxy=proxy,
                log=log,
            )
        if result.ok or result.already_bound:
            return result
        log(f"[绑2FA] 复用会话未果：{result.summary()}")
    else:
        log("[绑2FA] 库里没有 session_token / access_token，跳过会话复用")

    if not allow_login:
        return TwoFactorBindResult(error_message=f"{result.summary()}；已关闭协议重登")
    if not password:
        return TwoFactorBindResult(error_message=f"{result.summary()}；库里没有密码，无法协议重登")

    log(f"[绑2FA] 改走协议重登: {email}")
    mail_provider = _resolve_mail_provider(
        email,
        extra=extra,
        config=config,
        proxy=proxy,
        log=log,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    with mirror_protocol_logs(log_fn):
        return bind_totp_via_login(
            Config(proxy=(proxy or "").strip() or None),
            email,
            password,
            mail_provider=mail_provider,
            env_overrides={"OTP_TIMEOUT": str(_otp_timeout(config)), "WEBUI_ALLOW_LOGIN": "1"},
        )


def build_extra_patch(result: TwoFactorBindResult) -> dict[str, Any]:
    """把绑定结果整理成可以合并进 ``extra_json`` 的补丁。

    没拿到密钥就只留一条留痕：用空串覆盖掉库里原有的 totp_secret 等于把号的
    2FA 弄成永久锁死状态。
    """
    patch: dict[str, Any] = {}
    if result.secret:
        patch["totp_secret"] = result.secret
    patch["chatgpt_2fa"] = {
        "bound": result.ok or result.already_bound,
        "message": result.summary(),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return patch


def _bind_via_session(
    *,
    email: str,
    session_token: str,
    access_token: str,
    device_id: str,
    proxy: Optional[str],
    log: Callable[[str], None],
) -> TwoFactorBindResult:
    log(f"[绑2FA] 尝试复用已有会话: {email}")
    try:
        flow = AuthFlow(Config(proxy=(proxy or "").strip() or None))
        flow.from_existing_credentials(session_token, access_token, device_id)
    except Exception as exc:  # noqa: BLE001
        return TwoFactorBindResult(error_message=f"恢复会话失败: {exc}")
    if not flow.result.access_token:
        return TwoFactorBindResult(error_message="库里的 session/access token 已失效")
    return bind_totp_inline(flow, flow.result.access_token)


def _resolve_mail_provider(
    email: str,
    *,
    extra: dict,
    config: dict,
    proxy: Optional[str],
    log: Callable[[str], None],
    task_control,
    attempt_id,
):
    from services.chatgpt_otp_mailbox import resolve_otp_mail_provider

    mail_provider, reason = resolve_otp_mail_provider(
        email,
        account_extra=extra,
        config=config,
        proxy=proxy,
        log_fn=log,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    if mail_provider is None:
        log(f"[绑2FA] {email} 暂时读不到收件箱（{reason}），需要邮箱验证码时会失败")
    else:
        log(f"[绑2FA] 收件通道: {getattr(mail_provider, 'display_name', '邮箱')} → {email}")
    return mail_provider


def _otp_timeout(config: dict) -> int:
    for key in ("mailbox_otp_timeout_seconds", "email_otp_timeout_seconds", "otp_timeout"):
        try:
            seconds = int(str(config.get(key) or "").strip())
        except ValueError:
            continue
        if seconds > 0:
            return seconds
    return 180


def _load_config() -> dict:
    from core.config_store import config_store

    return config_store.get_all() or {}
