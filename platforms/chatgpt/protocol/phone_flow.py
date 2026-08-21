"""手机号注册链路，以及注册后把邮箱绑到账号上的那几步。

和邮箱注册的区别只在"身份"这一段：``authorize/continue`` 提交的
``username.kind`` 是 ``phone_number``，验证码走短信而不是邮件。身份验完之后
（create_account → workspace/select → callback → session → Codex）两条链完全
一样，所以这里只实现前半段，后半段调 ``AuthFlow`` 已有的方法。

绑定邮箱是独立的一段，OpenAI 把它放在 authorize 流程内部（页面 ``/add-email``）：

    POST /api/accounts/add-email/send      {"email": "..."}   不带 sentinel
    POST /api/accounts/email-otp/validate  {"code": "123456"} 不带 sentinel

第二步和邮箱注册用的是同一个接口，所以直接复用 ``AuthFlow.verify_otp``。
绑定只在服务端确实处于 add-email 步骤时才可能成功，其余时刻会被判 invalid
state —— 这类失败不该把已经注册好的号一起判死，调用方按"号留下、绑定失败"
处理。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from platforms.chatgpt.protocol.mail_provider import MailProvider

logger = logging.getLogger(__name__)

PHONE_USERNAME_KIND = "phone_number"

# 这个号不能用，换下一个再试
_PHONE_REJECTED_PATTERNS = (
    "phone_number_already_in_use",
    "already_in_use",
    "already_taken",
    "phone_already_verified",
    "already_verified",
    "disallowed_phone",
    "invalid_phone_number",
    "phone_number_invalid",
    "blocked_phone",
    "phone_number_blocked",
    "suspicious behavior from phone",
)

# 和号码无关的服务端状态错，换号只是按秒烧租号额度
_FLOW_STATE_PATTERNS = (
    "invalid authorization step",
    "invalid_authorization_step",
    "invalid state",
    "invalid_state",
)


def _matches(text: str, patterns: tuple) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in patterns)


def is_phone_rejected_error(text: str) -> bool:
    return _matches(text, _PHONE_REJECTED_PATTERNS)


def is_flow_state_error(text: str) -> bool:
    return _matches(text, _FLOW_STATE_PATTERNS)


class PhoneRegisterMixin:
    """手机号注册 + 绑定邮箱。混进 ``AuthFlow``，共用同一个 session 和结果对象。"""

    # ── 状态判定 ──

    @staticmethod
    def _is_add_email_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_email") or ("/add-email" in cu)

    @staticmethod
    def _is_phone_otp_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "phone_otp_verification") or ("phone-verification" in cu)

    @staticmethod
    def _is_create_password_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "create_account_password") or ("/create-account/password" in cu)

    @staticmethod
    def _is_existing_identity_state(page_type: str = "", continue_url: str = "") -> bool:
        """服务端认这个号已经有账号了（要密码或要登录验证码）。"""
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("login_password", "mfa_challenge")
            or "/log-in/password" in cu
            or "/mfa-challenge/" in cu
        )

    # ── 手机号身份 ──

    def phone_authorize_continue(self, phone_number: str, sentinel_token: str) -> dict:
        """把手机号当成注册身份提交。"""
        return self.authorize_continue(
            email=phone_number,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/log-in?usernameKind=phone_number",
            trace_step="authorize_continue_phone_signup",
            username_kind=PHONE_USERNAME_KIND,
        )

    def phone_register_user(self, phone_number: str) -> dict:
        """``POST user/register``，用户名就是手机号。成功即代表账号已在 OpenAI 侧建好。"""
        password = self._random_password()
        self.result.password = password

        if self.result.device_id:
            try:
                from platforms.chatgpt.protocol.sentinel import get_sentinel_token as _get_st

                token, so_token = _get_st(
                    self.session,
                    device_id=self.result.device_id,
                    flow="username_password_create",
                    **self._sentinel_fp_kwargs(),
                )
                self._last_sentinel_token = token or ""
                if so_token:
                    self._last_sentinel_so_token = so_token
            except Exception as exc:
                logger.warning("注册前刷新 sentinel 失败，沿用现有 token 提交: %s", exc)

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": phone_number},
            timeout=30,
        )
        self._trace_http("phone_register_user", resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"手机号注册失败: HTTP {resp.status_code} - {(resp.text or '')[:220]}"
            )

        # 密码只活在内存里，后面任何一步挂掉这个号就再也登不进去了，先落盘。
        if self._on_password is not None:
            try:
                self._on_password(phone_number, password)
            except Exception as exc:
                logger.warning("密码落盘回调失败（不影响注册，日志里还有兜底）: %s", exc)

        try:
            return resp.json() or {}
        except Exception:
            return {}

    # ── 绑定邮箱 ──

    def add_email_send(self, email: str) -> dict:
        """把邮箱提交给 add-email 步骤，OpenAI 会往这个地址发 6 位验证码。"""
        headers = self._common_headers("https://auth.openai.com/add-email")
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/add-email/send",
            headers=headers,
            json={"email": email},
            timeout=30,
        )
        self._trace_http("add_email_send", resp)
        if resp.status_code != 200:
            try:
                error = (resp.json() or {}).get("error") or {}
                message = error.get("message") or error.get("code") or ""
            except Exception:
                message = ""
            raise RuntimeError(
                message or f"add-email/send 失败: HTTP {resp.status_code} - {(resp.text or '')[:220]}"
            )
        try:
            return resp.json() or {}
        except Exception:
            return {}

    def bind_email(self, mail_provider: MailProvider, continue_url: str = "") -> str:
        """要一个邮箱 → 发码 → 验码。返回下一步的 continue_url。"""
        email = mail_provider.create_mailbox()
        logger.info("[绑定邮箱] 准备把 %s 绑到当前账号", email)

        sent_at = time.time()
        send_resp = self.add_email_send(email)
        next_url = self._normalize_continue_url(
            self._extract_continue_url_from_step(send_resp)
        )
        page_type = self._extract_page_type(send_resp)
        if not self._is_email_verification_state(page_type, next_url):
            logger.warning(
                "[绑定邮箱] add-email/send 未进入邮箱验证页: page=%s continue=%s",
                page_type or "(empty)",
                (next_url or "")[:160],
            )

        try:
            otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=sent_at)
        try:
            validate_resp = self.verify_otp(code)
        except RuntimeError as exc:
            if not any(status in str(exc) for status in ("401", "409")):
                raise
            # 错码/过期码：重新发一封再等一次，别为此丢掉整个号
            logger.warning("[绑定邮箱] 验证码校验失败，重发一次再试: %s", exc)
            sent_at = time.time()
            self.add_email_send(email)
            code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=sent_at)
            validate_resp = self.verify_otp(code)

        self.result.bound_email = email
        logger.info("[绑定邮箱] %s 绑定成功", email)
        return self._normalize_continue_url(
            self._extract_continue_url_from_step(validate_resp)
        ) or next_url or continue_url or ""

    @staticmethod
    def _is_email_verification_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("email_otp_verification", "external_url")
            or "email-verification" in cu
        )

    def _try_bind_email(self, mail_provider: Optional[MailProvider], continue_url: str) -> str:
        """绑定失败不判死账号：号已经注册好了，缺个邮箱而已。"""
        if mail_provider is None:
            return continue_url
        if self.result.bound_email:
            return continue_url
        try:
            return self.bind_email(mail_provider, continue_url=continue_url)
        except Exception as exc:
            self._bind_email_error = str(exc)
            logger.warning("[绑定邮箱] 失败（账号本身已注册成功，可稍后重试绑定）: %s", exc)
            return continue_url

    # ── 主流程 ──

    def run_phone_register(
        self,
        mail_provider: Optional[MailProvider] = None,
        bind_email: bool = False,
    ) -> Any:
        """用接码平台的手机号注册一个账号，可选把邮箱绑上去。"""
        if self._sms_callback is None:
            raise RuntimeError(
                "手机注册需要接码平台：请先在「设置 → 接码」里启用并填好 API Key"
            )
        self._bind_email_error = ""
        binder = mail_provider if bind_email else None

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        ctrl = self._sms_callback
        try:
            ctrl.set_resend_callback(self._phone_otp_resend)
        except Exception:
            pass

        try:
            continue_url, auth_url = self._do_phone_register_loop(ctrl)
        finally:
            for cleanup in (getattr(ctrl, "cleanup", None), getattr(ctrl, "_release_lock", None)):
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        pass

        # 手机验完之后服务端可能直接把 add-email 摆在下一步
        if self._is_add_email_state(continue_url=continue_url):
            continue_url = self._try_bind_email(binder, continue_url)

        if (not continue_url) or "/about-you" in continue_url:
            continue_url = self.create_account()
            if self._is_add_email_state(continue_url=continue_url):
                continue_url = self._try_bind_email(binder, continue_url)

        # 服务端没主动要求绑邮箱时，也在跟重定向链之前试一次：这一步只在
        # authorize 流程还停在 add-email 上时才会被接受，被拒了就当没绑。
        continue_url = self._try_bind_email(binder, continue_url)

        return self._finish_authorized_flow(continue_url, auth_url, mail_provider)

    def _do_phone_register_loop(self, ctrl) -> tuple:
        """一个号一个号地试：租号 → 提交身份 → 注册 → 收短信 → 验码。

        返回 ``(continue_url, auth_url)``；auth_url 留给后面 reauthorize 兜底用。
        """
        ctrl_cfg = getattr(ctrl, "config", None) or {}

        def _read_int(cfg_key: str, env_key: str, default: str, min_v: int = 1) -> int:
            raw = str(ctrl_cfg.get(cfg_key) or "").strip()
            if not raw:
                raw = self._get_env(env_key, default)
            try:
                return max(min_v, int(raw))
            except Exception:
                return int(default)

        per_phone_timeout = max(
            40, _read_int("sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT", "120", min_v=40)
        )
        max_phone_attempts = _read_int("sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS", "3")
        max_code_retries = _read_int(
            "sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES", "2"
        )

        logger.info(
            "[手机注册] 配置: 单号窗口=%ds 最多换号=%d 单号内验证重试=%d",
            per_phone_timeout,
            max_phone_attempts,
            max_code_retries,
        )

        last_err: Optional[Exception] = None
        repeated_err = ""
        repeated_count = 0

        for attempt in range(1, max_phone_attempts + 1):
            logger.info("[手机注册] 🔁 第 %d/%d 个号尝试...", attempt, max_phone_attempts)
            try:
                phone = ctrl.get_phone()
            except Exception as exc:
                last_err = exc
                logger.warning("[手机注册] 第 %d 个号租号失败: %s", attempt, exc)
                continue
            if not phone:
                last_err = RuntimeError("接码平台未返回手机号")
                continue

            self.result.phone_number = phone
            self.result.email = phone

            try:
                continue_url, auth_url = self._register_with_phone(phone, ctrl, per_phone_timeout, max_code_retries)
            except _PhoneUnusable as exc:
                last_err = exc.cause
                logger.warning("[手机注册] 号 %s 不可用，换下一个: %s", phone, str(exc.cause)[:200])
                try:
                    ctrl.mark_send_failed(str(exc.cause))
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                continue
            except _PhoneFlowBroken as exc:
                logger.warning(
                    "[手机注册] 服务端流程状态已失效（%s）：这不是号码问题，本轮到此为止",
                    str(exc.cause)[:200],
                )
                try:
                    ctrl.mark_send_failed(str(exc.cause))
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                raise exc.cause
            except Exception as exc:
                last_err = exc
                text = str(exc)[:300]
                logger.warning("[手机注册] 号 %s 失败（未识别错误）: %s", phone, text)
                try:
                    ctrl.mark_send_failed(text)
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                if text == repeated_err:
                    repeated_count += 1
                else:
                    repeated_err = text
                    repeated_count = 1
                if repeated_count >= 3:
                    logger.warning(
                        "[手机注册] 同一个错误连续 %d 个号了，判定与号码无关，停止换号",
                        repeated_count,
                    )
                    break
                continue

            try:
                ctrl.report_success()
            except Exception:
                pass
            return continue_url, auth_url

        if last_err:
            raise last_err
        raise RuntimeError(f"手机注册 {max_phone_attempts} 个号均失败")

    def _cleanup_phone(self, ctrl) -> None:
        """退掉当前这个号，下一轮 get_phone 才会租新的。"""
        try:
            ctrl.cleanup()
        except Exception:
            pass

    def _register_with_phone(
        self,
        phone: str,
        ctrl,
        per_phone_timeout: int,
        max_code_retries: int,
    ) -> tuple:
        """单个号码的完整尝试。号本身不行就抛 ``_PhoneUnusable``。"""
        # 每个号都重开一条 authorize 链：login_hint 带着号码，服务端状态也干净
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, login_hint=phone)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        try:
            data = self.phone_authorize_continue(phone, sentinel)
        except Exception as exc:
            if is_phone_rejected_error(str(exc)):
                raise _PhoneUnusable(exc)
            if is_flow_state_error(str(exc)):
                raise _PhoneFlowBroken(exc)
            raise

        page_type = self._extract_page_type(data)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(data))

        if self._is_existing_identity_state(page_type, continue_url):
            raise _PhoneUnusable(RuntimeError(f"手机号 {phone} 已被注册过（phone_number_already_in_use）"))

        if self._is_create_password_state(page_type, continue_url):
            register_resp = self.phone_register_user(phone)
            page_type = self._extract_page_type(register_resp)
            continue_url = self._normalize_continue_url(
                self._extract_continue_url_from_step(register_resp)
            )

        if not self._is_phone_otp_state(page_type, continue_url):
            # 服务端没自动进短信验证页时，主动触发一次发码
            logger.info(
                "[手机注册] 未直接进入短信验证页 (page=%s)，主动触发 add-phone/send",
                page_type or "(empty)",
            )
            try:
                send_resp = self._add_phone_send(phone)
            except Exception as exc:
                if is_phone_rejected_error(str(exc)):
                    raise _PhoneUnusable(exc)
                if is_flow_state_error(str(exc)):
                    raise _PhoneFlowBroken(exc)
                raise
            page_type = self._extract_page_type(send_resp)
            continue_url = self._normalize_continue_url(
                self._extract_continue_url_from_step(send_resp)
            )
            if not self._is_phone_otp_state(page_type, continue_url):
                raise RuntimeError(
                    f"提交手机号后未进入短信验证页: page={page_type or '(empty)'}"
                )

        try:
            ctrl.mark_send_succeeded()
        except Exception:
            pass

        return self._wait_and_validate_sms(phone, ctrl, per_phone_timeout, max_code_retries, continue_url), auth_url

    def _wait_and_validate_sms(
        self,
        phone: str,
        ctrl,
        per_phone_timeout: int,
        max_code_retries: int,
        continue_url: str,
    ) -> str:
        started = time.time()
        seen_codes: set = set()
        code_attempt = 0
        last_err: Optional[Exception] = None

        while time.time() - started < per_phone_timeout and code_attempt < max_code_retries:
            remaining = per_phone_timeout - (time.time() - started)
            if remaining < 10:
                break
            code_attempt += 1
            logger.info(
                "[手机注册] 号 %s 第 %d/%d 次等短信 (剩余 %ds)",
                phone,
                code_attempt,
                max_code_retries,
                int(remaining),
            )
            code = ctrl.get_code(timeout=int(remaining))
            if not code:
                break
            if code in seen_codes:
                logger.warning("[手机注册] 收到重复验证码 %s，跳过", code)
                continue
            seen_codes.add(code)

            try:
                validate_resp = self._phone_otp_validate(code)
            except Exception as exc:
                last_err = exc
                logger.warning("[手机注册] 短信验证码校验失败 (code=%s): %s", code, str(exc)[:200])
                try:
                    ctrl.mark_code_failed(str(exc))
                except Exception:
                    pass
                continue

            next_url = self._normalize_continue_url(
                self._extract_continue_url_from_step(validate_resp)
            )
            logger.info("[手机注册] ✅ 手机号 %s 验证通过", phone)
            return next_url or continue_url or ""

        raise _PhoneUnusable(last_err or TimeoutError(f"号 {phone} 在 {per_phone_timeout}s 内没收到短信"))

    def _finish_authorized_flow(
        self,
        continue_url: str,
        auth_url: str,
        mail_provider: Optional[MailProvider],
    ) -> Any:
        """身份验完之后的收尾：跟重定向链拿 callback、换 session、换 refresh_token。"""
        continue_url = self._normalize_continue_url(continue_url or "")
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if (not refresh_only_mode) and callback_url:
            self._consume_callback_for_session(callback_url)

        if not refresh_only_mode:
            self.get_auth_session()

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_phone_register")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("手机注册流程完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("手机注册流程完成，但未拿到有效凭证")

        logger.info("手机注册流程完成!")
        return self.result


class _PhoneUnusable(Exception):
    """这个号码不能用，换下一个。"""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


class _PhoneFlowBroken(Exception):
    """服务端流程状态坏了，换号没意义。"""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause
