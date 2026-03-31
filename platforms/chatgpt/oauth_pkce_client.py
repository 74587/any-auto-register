"""
OAuth PKCE 注册客户端

完整实现 auth.openai.com 注册状态机 + 登录获取 Token 的全生命周期。
每个步骤封装为独立方法，调用方按编号依次调用即可完成整个注册流程。
"""

import json
import re
import time
import urllib.parse
from typing import Optional

from curl_cffi import requests as curl_requests

from .oauth import (
    OAuthStart,
    _decode_jwt_segment,
    generate_oauth_url,
    submit_callback_url,
)

AUTH_BASE = "https://auth.openai.com"
SENTINEL_API = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6"
CLOUDFLARE_TRACE = "https://cloudflare.com/cdn-cgi/trace"


class OAuthPkceClient:
    """
    OAuth PKCE 注册客户端

    完整注册流程（12 步）：
      1.  检查 IP 地区
      2.  访问 OAuth 授权 URL，获取 oai-did Cookie
      3.  获取 Sentinel Token
      4.  提交邮箱 (authorize/continue)
      5.  提交密码 (user/register)
      6.  发送 OTP (email-otp/send)
      7.  验证 OTP (email-otp/validate)
      8.  创建账户 (create_account)
      9.  注册后重新 OAuth 登录
      10. 解析 workspace_id
      11. 选择 workspace
      12. 跟踪重定向链，交换 OAuth code → access_token
    """

    def __init__(self, proxy: Optional[str] = None, log_fn=None):
        self.proxy = proxy
        self._log = log_fn or (lambda msg: None)
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

        # 主会话：贯穿整个注册 + 登录流程
        self.session = curl_requests.Session(
            proxies=self._proxies,
            impersonate="chrome",
        )

        self._device_id: Optional[str] = None
        self._sentinel: Optional[str] = None
        self._consent_url: str = ""
        self._workspace_session_data: Optional[dict] = None
        self._create_account_continue_url: Optional[str] = None
        self._create_account_workspace_id: Optional[str] = None
        self._create_account_refresh_token: Optional[str] = None
        self._create_account_page_type: Optional[str] = None
        self._last_validate_otp_continue_url: Optional[str] = None
        self._last_validate_otp_workspace_id: Optional[str] = None

    # ══════════════════════════════════════════════════════════════════
    # 内部方法：获取 Sentinel Token（极简模式）
    # ══════════════════════════════════════════════════════════════════

    def _fetch_sentinel_token(self, device_id: str, flow: str = "authorize_continue") -> str:
        """
        获取 Sentinel Token。

        使用独立连接（不复用 session cookie），请求体 p 字段留空，
        只取响应中的 token 字段拼装为 openai-sentinel-token header 值。

        Returns:
            JSON 格式的 sentinel token 字符串。
        """
        req_body = json.dumps({"p": "", "id": device_id, "flow": flow})

        resp = curl_requests.post(
            SENTINEL_API,
            headers={
                "origin": "https://sentinel.openai.com",
                "referer": SENTINEL_REFERER,
                "content-type": "text/plain;charset=UTF-8",
            },
            data=req_body,
            proxies=self._proxies,
            impersonate="chrome",
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Sentinel 获取失败: HTTP {resp.status_code}")

        c_value = resp.json().get("token", "")
        if not c_value:
            raise RuntimeError("Sentinel 响应缺少 token 字段")

        return json.dumps({
            "p": "", "t": "", "c": c_value,
            "id": device_id, "flow": flow,
        }, separators=(",", ":"))

    # ══════════════════════════════════════════════════════════════════
    # 步骤 1：检查 IP 地区
    # ══════════════════════════════════════════════════════════════════

    def check_ip_region(self) -> str:
        """检查当前 IP 地区，CN/HK 不支持。"""
        try:
            resp = self.session.get(CLOUDFLARE_TRACE, timeout=10)
            match = re.search(r"^loc=(.+)$", resp.text, re.MULTILINE)
            loc = match.group(1).strip() if match else "UNKNOWN"
            self._log(f"当前 IP 地区: {loc}")
            if loc in ("CN", "HK"):
                raise RuntimeError(f"IP 地区不支持: {loc}")
            return loc
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"IP 地区检查失败: {e}") from e

    # ══════════════════════════════════════════════════════════════════
    # 步骤 2：访问 OAuth 授权 URL，获取 oai-did Cookie
    # ══════════════════════════════════════════════════════════════════

    def init_oauth_session(self) -> OAuthStart:
        """生成 OAuth PKCE URL 并访问，建立 auth.openai.com 会话。"""
        oauth = generate_oauth_url()
        self._log("访问 OAuth 授权 URL...")
        self.session.get(oauth.auth_url, timeout=15)
        self._device_id = self.session.cookies.get("oai-did") or ""
        self._log(f"oai-did: {self._device_id[:16]}..." if self._device_id else "oai-did: (未获取到)")
        return oauth

    # ══════════════════════════════════════════════════════════════════
    # 步骤 3：获取 Sentinel Token
    # ══════════════════════════════════════════════════════════════════

    def refresh_sentinel(self) -> str:
        """获取新的 Sentinel Token 并缓存。"""
        if not self._device_id:
            raise RuntimeError("尚未初始化 oai-did（请先调用 init_oauth_session）")
        self._sentinel = self._fetch_sentinel_token(self._device_id)
        self._log("Sentinel Token 已获取")
        return self._sentinel

    @staticmethod
    def _extract_continue_info(resp) -> tuple[str, str]:
        """从接口响应中提取 page.type 和 continue_url。"""
        try:
            data = resp.json() or {}
        except Exception:
            return "", ""

        page_type = str(((data.get("page") or {}).get("type") or "")).strip()
        continue_url = str(data.get("continue_url") or "").strip()
        return page_type, continue_url

    @staticmethod
    def _extract_workspace_id_from_payload(payload) -> str:
        if not isinstance(payload, dict):
            return ""
        workspace_id = str(
            payload.get("workspace_id")
            or payload.get("workspaceId")
            or payload.get("default_workspace_id")
            or ((payload.get("workspace") or {}).get("id") if isinstance(payload.get("workspace"), dict) else "")
            or ""
        ).strip()
        if workspace_id:
            return workspace_id
        workspaces = payload.get("workspaces") or []
        if isinstance(workspaces, list) and workspaces:
            return str((workspaces[0] or {}).get("id") or "").strip()
        return ""

    @staticmethod
    def _iter_candidate_dicts(payload, max_depth: int = 4):
        stack = [(payload, 0)]
        seen = set()
        while stack:
            item, depth = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, dict):
                yield item
                if depth < max_depth:
                    for value in item.values():
                        if isinstance(value, (dict, list, tuple)):
                            stack.append((value, depth + 1))
            elif isinstance(item, (list, tuple)) and depth < max_depth:
                for value in item:
                    if isinstance(value, (dict, list, tuple)):
                        stack.append((value, depth + 1))

    def _extract_workspace_and_continue_from_payload(self, payload, base_url: str = "") -> tuple[str, str]:
        workspace_id = ""
        continue_url = ""
        for item in self._iter_candidate_dicts(payload):
            if not workspace_id:
                workspace_id = self._extract_workspace_id_from_payload(item)
            if not continue_url:
                for key in ("continue_url", "continueUrl", "next_url", "nextUrl", "redirect_url", "redirectUrl", "url"):
                    candidate = str(item.get(key) or "").strip()
                    if not candidate:
                        continue
                    if candidate.startswith("/") and base_url:
                        candidate = urllib.parse.urljoin(base_url, candidate)
                    continue_url = candidate
                    break
            if workspace_id and continue_url:
                break
        return workspace_id, continue_url

    @staticmethod
    def _is_registration_gate_url(url: str) -> bool:
        target = str(url or "").lower()
        return any(marker in target for marker in ("/about-you", "/add-phone", "/email-verification"))

    @staticmethod
    def _is_workspace_resolution_url(url: str) -> bool:
        target = str(url or "").lower()
        return any(marker in target for marker in ("sign-in-with-chatgpt", "consent", "workspace", "organization"))

    # ══════════════════════════════════════════════════════════════════
    # 步骤 4：提交邮箱
    # ══════════════════════════════════════════════════════════════════

    def submit_email(self, email: str) -> dict:
        """向 authorize/continue 提交邮箱，触发注册状态机。"""
        if not self._sentinel:
            raise RuntimeError("Sentinel Token 未初始化")

        payload = json.dumps({
            "username": {"value": email, "kind": "email"},
            "screen_hint": "signup",
        })
        self._log(f"提交邮箱: {email}")

        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/authorize/continue",
            headers={
                "referer": f"{AUTH_BASE}/create-account",
                "accept": "application/json",
                "content-type": "application/json",
                "openai-sentinel-token": self._sentinel,
            },
            data=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"提交邮箱失败: HTTP {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        self._log(f"邮箱提交成功")
        return data

    # ══════════════════════════════════════════════════════════════════
    # 步骤 5：提交密码
    # ══════════════════════════════════════════════════════════════════

    def submit_password(self, email: str, password: str) -> str:
        """向 user/register 提交密码，返回 continue_url。"""
        payload = json.dumps({"password": password, "username": email})
        self._log("提交密码...")

        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/user/register",
            headers={
                "referer": f"{AUTH_BASE}/create-account/password",
                "accept": "application/json",
                "content-type": "application/json",
                "openai-sentinel-token": self._sentinel or "",
            },
            data=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"提交密码失败: HTTP {resp.status_code} {resp.text[:300]}")

        continue_url = resp.json().get("continue_url") or ""
        self._log(f"密码提交成功{', continue_url 已获取' if continue_url else ''}")
        return continue_url

    # ══════════════════════════════════════════════════════════════════
    # 步骤 6：发送 OTP
    # ══════════════════════════════════════════════════════════════════

    def send_otp(self, continue_url: str = "") -> bool:
        """触发发送邮箱验证码。"""
        url = continue_url or f"{AUTH_BASE}/api/accounts/email-otp/send"
        self._log(f"发送验证码: {url}")

        try:
            resp = self.session.post(
                url,
                headers={
                    "referer": f"{AUTH_BASE}/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "openai-sentinel-token": self._sentinel or "",
                },
                timeout=30,
            )
            self._log(f"验证码发送状态: {resp.status_code}")
            return resp.status_code == 200
        except Exception as e:
            self._log(f"发送验证码异常（非致命）: {e}")
            return False

    # ══════════════════════════════════════════════════════════════════
    # 步骤 7：验证 OTP
    # ══════════════════════════════════════════════════════════════════

    def validate_otp(self, code: str) -> None:
        """提交邮箱验证码。"""
        self._log(f"验证 OTP: {code}")

        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            headers={
                "referer": f"{AUTH_BASE}/email-verification",
                "accept": "application/json",
                "content-type": "application/json",
            },
            data=json.dumps({"code": code}),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OTP 验证失败: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json() or {}
            found_workspace, found_continue = self._extract_workspace_and_continue_from_payload(
                data,
                base_url=f"{AUTH_BASE}/api/accounts/email-otp/validate",
            )
            if found_workspace:
                self._last_validate_otp_workspace_id = found_workspace
                self._log(f"OTP 校验返回 Workspace ID: {found_workspace}")
            if found_continue:
                self._last_validate_otp_continue_url = found_continue
                self._log(
                    f"OTP 校验返回 continue_url: "
                    f"{(found_continue[:160] + '...') if len(found_continue) > 160 else found_continue}"
                )
        except Exception as e:
            self._log(f"解析 OTP 校验返回信息失败: {e}")
        self._log("OTP 验证通过")

    # ══════════════════════════════════════════════════════════════════
    # 步骤 8：创建账户
    # ══════════════════════════════════════════════════════════════════

    def create_account(self, name: str, birthdate: str) -> None:
        """提交姓名和生日完成账户创建。"""
        self._log(f"创建账户: {name} ({birthdate})")

        headers = {
            "referer": f"{AUTH_BASE}/about-you",
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self._sentinel:
            headers["openai-sentinel-token"] = self._sentinel

        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/create_account",
            headers=headers,
            data=json.dumps({"name": name, "birthdate": birthdate}),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"创建账户失败: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json() or {}
            found_workspace, found_continue = self._extract_workspace_and_continue_from_payload(
                data,
                base_url=f"{AUTH_BASE}/api/accounts/create_account",
            )
            if found_continue:
                self._create_account_continue_url = found_continue
                self._log(
                    f"create_account 返回 continue_url，已缓存: "
                    f"{(found_continue[:160] + '...') if len(found_continue) > 160 else found_continue}"
                )
            page_info = data.get("page") if isinstance(data, dict) else None
            if isinstance(page_info, dict):
                page_type = str(page_info.get("type") or "").strip()
                if page_type:
                    self._create_account_page_type = page_type
                    self._log(f"create_account 返回 page.type: {page_type}")
            if found_workspace:
                self._create_account_workspace_id = found_workspace
                self._log(f"create_account 返回 workspace_id，已缓存: {found_workspace}")
            refresh_token = str(data.get("refresh_token") or "").strip()
            if refresh_token:
                self._create_account_refresh_token = refresh_token
                self._log("create_account 返回 refresh_token，已缓存")
        except Exception as e:
            self._log(f"解析 create_account 响应失败: {e}")
        self._log("账户创建成功")

    # ══════════════════════════════════════════════════════════════════
    # 步骤 9：注册后重新 OAuth 登录
    # ══════════════════════════════════════════════════════════════════

    def login_after_register(
        self, email: str, password: str, otp_code: str = ""
    ) -> OAuthStart:
        """
        注册完成后重走 OAuth 登录流程。

        注册阶段的 session 不含 workspace 信息，必须重新走一次
        OAuth 登录获取 oai-client-auth-session Cookie。

        Returns:
            登录阶段的 OAuthStart（含 code_verifier 等，用于步骤 12 Token 交换）。
        """
        self._log("=" * 40)
        self._log("开始 OAuth 登录（获取 workspace）...")

        # 9-1. 访问新 OAuth URL
        login_oauth = generate_oauth_url()
        self.session.get(login_oauth.auth_url, timeout=15)
        login_did = self.session.cookies.get("oai-did") or self._device_id or ""
        self._log(f"登录阶段 oai-did: {login_did[:16]}..." if login_did else "登录阶段 oai-did: (空)")

        # 9-2. 获取登录阶段 Sentinel
        login_sentinel = self._fetch_sentinel_token(login_did)

        # 9-3. 提交邮箱（screen_hint=login）
        login_email_resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/authorize/continue",
            headers={
                "referer": f"{AUTH_BASE}/sign-in",
                "accept": "application/json",
                "content-type": "application/json",
                "openai-sentinel-token": login_sentinel,
            },
            data=json.dumps({
                "username": {"value": email, "kind": "email"},
                "screen_hint": "login",
            }),
            timeout=30,
        )
        if login_email_resp.status_code != 200:
            raise RuntimeError(f"登录提交邮箱失败: HTTP {login_email_resp.status_code}")

        page_type, continue_url = self._extract_continue_info(login_email_resp)
        self._log(f"登录页面类型: {page_type}")
        self._log(
            f"登录提交邮箱响应: page={page_type or '(空)'} "
            f"continue_url={(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
        )

        # 9-4. 提交密码（login_password 页面）
        if "password" in page_type:
            self._log("提交密码...")
            pwd_resp = self.session.post(
                f"{AUTH_BASE}/api/accounts/password/verify",
                headers={
                    "referer": f"{AUTH_BASE}/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "openai-sentinel-token": login_sentinel,
                },
                data=json.dumps({"password": password}),
                timeout=30,
            )
            if pwd_resp.status_code != 200:
                raise RuntimeError(f"登录密码验证失败: HTTP {pwd_resp.status_code}")
            page_type, continue_url = self._extract_continue_info(pwd_resp)
            self._log(f"密码验证后页面类型: {page_type}")
            self._log(
                f"密码验证响应: page={page_type or '(空)'} "
                f"continue_url={(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
            )

        # 9-5. 二次 OTP（复用注册阶段验证码）
        if "otp" in page_type or "verification" in page_type:
            if not otp_code:
                raise RuntimeError("登录需要二次 OTP 验证，但未提供验证码")
            self._log(f"提交登录二次验证码: {otp_code}")
            # 触发发信请求以满足后端状态机（可忽略报错）
            try:
                self.session.post(
                    f"{AUTH_BASE}/api/accounts/passwordless/send-otp",
                    headers={
                        "referer": f"{AUTH_BASE}/log-in/password",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    timeout=10,
                )
            except Exception:
                pass

            otp_resp = self.session.post(
                f"{AUTH_BASE}/api/accounts/email-otp/validate",
                headers={
                    "referer": f"{AUTH_BASE}/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                    "openai-sentinel-token": login_sentinel,
                },
                data=json.dumps({"code": otp_code}),
                timeout=30,
            )
            if otp_resp.status_code != 200:
                raise RuntimeError(f"登录二次 OTP 失败: HTTP {otp_resp.status_code} {otp_resp.text[:200]}")
            page_type, continue_url = self._extract_continue_info(otp_resp)
            self._log("登录二次验证通过")
            self._log(
                f"登录二次 OTP 响应: page={page_type or '(空)'} "
                f"continue_url={(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
            )

        if continue_url:
            self._consent_url = continue_url
            self._log(f"登录后 continue_url: {continue_url}")
        else:
            self._consent_url = f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent"
            self._log("登录响应未返回 continue_url，回退使用默认 consent URL")

        self._workspace_session_data = None
        self._log("OAuth 登录流程完成")
        return login_oauth

    # ══════════════════════════════════════════════════════════════════
    # 步骤 10：解析 workspace_id
    # ══════════════════════════════════════════════════════════════════

    def extract_workspace_id(self) -> str:
        """优先从会话/consent 页面提取 workspace_id，失败再回退 Cookie 段解码。"""
        cached_workspace = str(self._last_validate_otp_workspace_id or "").strip()
        if cached_workspace:
            self._log(f"使用 OTP 返回的 Workspace ID: {cached_workspace}")
            return cached_workspace

        cached_workspace = str(self._create_account_workspace_id or "").strip()
        if cached_workspace:
            self._log(f"使用 create_account 缓存 Workspace ID: {cached_workspace}")
            return cached_workspace

        candidate_urls = []
        for candidate in (
            str(self._create_account_continue_url or "").strip(),
            str(self._consent_url or "").strip(),
            str(self._last_validate_otp_continue_url or "").strip(),
        ):
            if candidate and candidate not in candidate_urls:
                candidate_urls.append(candidate)

        for candidate_url in candidate_urls:
            if not self._is_workspace_resolution_url(candidate_url):
                self._log(f"跳过非 workspace 解析地址: {candidate_url}")
                continue
            self._log(f"尝试从地址解析 workspace: {candidate_url}")
            session_data = self._load_workspace_session_data(candidate_url)
            workspaces = (session_data or {}).get("workspaces") or []
            if workspaces:
                wid = str((workspaces[0] or {}).get("id") or "").strip()
                if wid:
                    self._log(f"成功解析 workspace_id: {wid}")
                    return wid

        auth_cookie = ""
        try:
            auth_cookie = str(
                self.session.cookies.get("oai-client-auth-session", domain=".auth.openai.com")
                or self.session.cookies.get("oai-client-auth-session", domain="auth.openai.com")
                or self.session.cookies.get("oai-client-auth-session")
                or ""
            ).strip()
        except Exception:
            auth_cookie = str(self.session.cookies.get("oai-client-auth-session") or "").strip()

        if auth_cookie:
            import base64

            candidate_payloads = []
            segments = auth_cookie.split(".")
            if len(segments) >= 2 and segments[1]:
                candidate_payloads.append(segments[1])
            if segments and segments[0]:
                candidate_payloads.append(segments[0])
            candidate_payloads.append(auth_cookie)

            for payload in candidate_payloads:
                raw = str(payload or "").strip()
                if not raw:
                    continue
                auth_json = None
                try:
                    pad = "=" * ((4 - (len(raw) % 4)) % 4)
                    decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
                    auth_json = json.loads(decoded.decode("utf-8"))
                except Exception:
                    try:
                        auth_json = json.loads(raw)
                    except Exception:
                        auth_json = None

                wid = self._extract_workspace_id_from_payload(auth_json)
                if wid:
                    self._log(f"Workspace ID (auth-session): {wid}")
                    return wid

        auth_info_raw = ""
        try:
            auth_info_raw = str(
                self.session.cookies.get("oai-client-auth-info", domain=".auth.openai.com")
                or self.session.cookies.get("oai-client-auth-info", domain="auth.openai.com")
                or self.session.cookies.get("oai-client-auth-info")
                or ""
            ).strip()
        except Exception:
            auth_info_raw = str(self.session.cookies.get("oai-client-auth-info") or "").strip()

        if auth_info_raw:
            auth_info_text = auth_info_raw
            for _ in range(2):
                decoded = urllib.parse.unquote(auth_info_text)
                if decoded == auth_info_text:
                    break
                auth_info_text = decoded
            try:
                stripped = str(auth_info_text or "").strip()
                if stripped and (len(stripped) >= 2) and (stripped[0] == stripped[-1]) and (stripped[0] in ("'", '"')):
                    stripped = stripped[1:-1].strip()

                auth_info_json = None
                if stripped and stripped[0] in "{[":
                    auth_info_json = json.loads(stripped)
                else:
                    import base64

                    candidates_raw = []
                    if stripped:
                        candidates_raw.append(stripped)
                    if "." in stripped:
                        for seg in stripped.split("."):
                            seg = seg.strip()
                            if seg:
                                candidates_raw.append(seg)

                    for candidate in candidates_raw:
                        pad = "=" * ((4 - (len(candidate) % 4)) % 4)
                        decoded_candidates = []
                        try:
                            decoded_candidates.append(base64.urlsafe_b64decode((candidate + pad).encode("ascii")))
                        except Exception:
                            pass
                        try:
                            decoded_candidates.append(base64.b64decode((candidate + pad).encode("ascii")))
                        except Exception:
                            pass
                        for decoded in decoded_candidates:
                            try:
                                text = decoded.decode("utf-8")
                            except Exception:
                                continue
                            for _ in range(2):
                                decoded_text = urllib.parse.unquote(text)
                                if decoded_text == text:
                                    break
                                text = decoded_text
                            text = text.strip()
                            if text and text[0] in "{[":
                                auth_info_json = json.loads(text)
                                break
                        if auth_info_json is not None:
                            break

                wid = self._extract_workspace_id_from_payload(auth_info_json)
                if wid:
                    self._log(f"Workspace ID (auth-info): {wid}")
                    return wid
            except Exception as e:
                self._log(f"解析 auth-info Cookie 失败: {e}")

        decoded_cookie = self._decode_oauth_session_cookie() or {}
        if decoded_cookie:
            self._log(f"Cookie 字段: {list(decoded_cookie.keys())}")

        fallback_workspace = str(self._create_account_workspace_id or "").strip()
        if fallback_workspace:
            self._log(f"Workspace ID (create_account缓存): {fallback_workspace}")
            return fallback_workspace
        raise RuntimeError("无法从 Cookie/consent 中解析 workspace_id")

    # ══════════════════════════════════════════════════════════════════
    # 步骤 11：选择 workspace
    # ══════════════════════════════════════════════════════════════════

    def select_workspace(self, workspace_id: str) -> str:
        """选择 workspace；如需要再自动选择 organization，返回下一跳 continue_url。"""
        self._log(f"选择 workspace: {workspace_id}")

        resp = self.session.post(
            f"{AUTH_BASE}/api/accounts/workspace/select",
            headers={
                "referer": self._consent_url or f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent",
                "accept": "application/json",
                "content-type": "application/json",
            },
            data=json.dumps({"workspace_id": workspace_id}),
            timeout=30,
        )
        if resp.status_code not in (200, 301, 302, 303, 307, 308):
            raise RuntimeError(f"workspace/select 失败: HTTP {resp.status_code} {resp.text[:300]}")
        self._log(f"workspace/select 状态: {resp.status_code}")

        if resp.status_code in (301, 302, 303, 307, 308):
            continue_url = urllib.parse.urljoin(
                f"{AUTH_BASE}/api/accounts/workspace/select",
                resp.headers.get("Location") or "",
            )
            if continue_url:
                self._log(
                    f"workspace/select 返回重定向: "
                    f"{(continue_url[:160] + '...') if len(continue_url) > 160 else continue_url}"
                )
                return continue_url
            raise RuntimeError("workspace/select 重定向缺少 Location")

        data = resp.json() or {}
        orgs = ((data.get("data") or {}).get("orgs") or [])
        continue_url = str(data.get("continue_url") or "").strip()
        self._log(
            f"workspace/select 响应: orgs={len(orgs)} "
            f"continue_url={(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
        )

        if orgs:
            org = orgs[0] or {}
            org_id = str(org.get("id") or "").strip()
            projects = org.get("projects") or []
            project_id = str((projects[0] or {}).get("id") or "").strip() if projects else ""
            if not org_id:
                raise RuntimeError("workspace/select 返回 orgs，但缺少 org_id")

            self._log(f"选择 organization: {org_id}")
            if project_id:
                self._log(f"选择 project: {project_id}")
            org_payload = {"org_id": org_id}
            if project_id:
                org_payload["project_id"] = project_id

            org_resp = self.session.post(
                f"{AUTH_BASE}/api/accounts/organization/select",
                headers={
                    "referer": continue_url or self._consent_url or f"{AUTH_BASE}/sign-in-with-chatgpt/codex/consent",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=json.dumps(org_payload),
                timeout=30,
            )
            if org_resp.status_code not in (200, 301, 302, 303, 307, 308):
                raise RuntimeError(f"organization/select 失败: HTTP {org_resp.status_code} {org_resp.text[:300]}")
            self._log(f"organization/select 状态: {org_resp.status_code}")

            if org_resp.status_code in (301, 302, 303, 307, 308):
                continue_url = urllib.parse.urljoin(
                    f"{AUTH_BASE}/api/accounts/organization/select",
                    org_resp.headers.get("Location") or "",
                )
                self._log(
                    f"organization/select 重定向: "
                    f"{(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
                )
            else:
                org_page_type, continue_url = self._extract_continue_info(org_resp)
                self._log(
                    f"organization/select 响应: page={org_page_type or '(空)'} "
                    f"continue_url={(continue_url[:160] + '...') if len(continue_url) > 160 else (continue_url or '(空)')}"
                )
        else:
            self._log("workspace/select 未返回 organization 列表")

        if not continue_url:
            raise RuntimeError("workspace/org 选择后缺少 continue_url")

        self._log("workspace 选择成功，continue_url 已获取")
        return continue_url

    def _load_workspace_session_data(self, consent_url: str = "") -> Optional[dict]:
        """优先从 cookie 读取 workspace，会话不足时回退到 consent HTML。"""
        if self._workspace_session_data and self._workspace_session_data.get("workspaces"):
            self._log(
                f"复用缓存 workspace session: "
                f"{len(self._workspace_session_data.get('workspaces', []))} 个 workspace"
            )
            return self._workspace_session_data

        session_data = self._decode_oauth_session_cookie()
        if session_data:
            self._log(
                f"Cookie session 已解码: keys={list(session_data.keys())} "
                f"workspaces={len(session_data.get('workspaces', []) or [])}"
            )
            if session_data.get("workspaces"):
                self._workspace_session_data = session_data
                return session_data
        else:
            self._log("Cookie session 解码失败或为空")

        self._log(
            f"Cookie 中无 workspace，回退抓取 consent HTML: "
            f"{(consent_url[:160] + '...') if len(consent_url) > 160 else (consent_url or self._consent_url or '(空)')}"
        )

        html = self._fetch_consent_page_html(consent_url)
        if not html:
            self._log("consent HTML 获取为空")
            return session_data

        parsed = self._extract_session_data_from_consent_html(html)
        if parsed and parsed.get("workspaces"):
            self._workspace_session_data = parsed
            self._log(
                f"从 consent HTML 提取到 {len(parsed.get('workspaces', []))} 个 workspace, "
                f"keys={list(parsed.keys())}"
            )
            return parsed

        self._log("consent HTML 中未提取到 workspace")

        return session_data

    def _fetch_consent_page_html(self, consent_url: str) -> str:
        """拉取 consent 页 HTML，供本地玩具环境解析 workspace 数据。"""
        target = consent_url or self._consent_url
        if not target:
            self._log("consent URL 为空，无法抓取 HTML")
            return ""

        try:
            self._log(
                f"抓取 consent HTML: "
                f"{(target[:160] + '...') if len(target) > 160 else target}"
            )
            resp = self.session.get(
                target,
                headers={
                    "referer": f"{AUTH_BASE}/email-verification",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                allow_redirects=False,
                timeout=30,
            )
            self._log(
                f"consent HTML 响应: status={resp.status_code} "
                f"content-type={resp.headers.get('content-type', '')}"
            )
            if resp.status_code == 200 and "text/html" in (resp.headers.get("content-type", "").lower()):
                return resp.text
            if resp.status_code in (301, 302, 303, 307, 308):
                self._log(f"consent HTML 重定向到: {resp.headers.get('Location', '')}")
        except Exception as e:
            self._log(f"获取 consent HTML 失败: {e}")
        return ""

    def _decode_oauth_session_cookie(self) -> Optional[dict]:
        """解码 oai-client-auth-session 中的 JSON 载荷。"""
        try:
            for cookie in self.session.cookies:
                if getattr(cookie, "name", "") != "oai-client-auth-session":
                    continue
                value = getattr(cookie, "value", "") or ""
                if value:
                    data = self._decode_cookie_json_value(value)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return None

    @staticmethod
    def _decode_cookie_json_value(value: str) -> Optional[dict]:
        import base64

        raw_value = str(value or "").strip()
        if not raw_value:
            return None

        candidates = [raw_value]
        if "." in raw_value:
            candidates.insert(0, raw_value.split(".", 1)[0])

        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            padded = candidate + "=" * (-len(candidate) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    decoded = decoder(padded).decode("utf-8")
                    parsed = json.loads(decoded)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed

        return None

    def get_cached_continue_url(self) -> str:
        """返回可用于继续授权链路的缓存 continue_url。"""
        candidates = [
            str(self._last_validate_otp_continue_url or "").strip(),
            str(self._create_account_continue_url or "").strip(),
            str(self._consent_url or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            if self._is_registration_gate_url(candidate):
                self._log(f"忽略注册门页 continue_url: {candidate}")
                continue
            return candidate
        return ""

    @staticmethod
    def _extract_session_data_from_consent_html(html: str) -> Optional[dict]:
        """从 consent HTML/stream 片段里提取 workspace 列表。"""
        if not html or "workspaces" not in html:
            return None

        def _first_match(patterns, text):
            for pattern in patterns:
                m = re.search(pattern, text, re.S)
                if m:
                    return m.group(1)
            return ""

        def _build_from_text(text):
            if not text or "workspaces" not in text:
                return None

            normalized = text.replace('\\"', '"')

            session_id = _first_match(
                [r'"session_id","([^"]+)"', r'"session_id":"([^"]+)"'],
                normalized,
            )
            client_id = _first_match(
                [r'"openai_client_id","([^"]+)"', r'"openai_client_id":"([^"]+)"'],
                normalized,
            )

            start = normalized.find('"workspaces"')
            if start < 0:
                start = normalized.find("workspaces")
            if start < 0:
                return None

            end = normalized.find('"openai_client_id"', start)
            if end < 0:
                end = normalized.find("openai_client_id", start)
            if end < 0:
                end = min(len(normalized), start + 4000)
            else:
                end = min(len(normalized), end + 600)

            workspace_chunk = normalized[start:end]
            ids = re.findall(r'"id"(?:,|:)"([0-9a-fA-F-]{36})"', workspace_chunk)
            if not ids:
                return None

            kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', workspace_chunk)
            workspaces = []
            seen = set()
            for idx, wid in enumerate(ids):
                if wid in seen:
                    continue
                seen.add(wid)
                item = {"id": wid}
                if idx < len(kinds):
                    item["kind"] = kinds[idx]
                workspaces.append(item)

            if not workspaces:
                return None

            return {
                "session_id": session_id,
                "openai_client_id": client_id,
                "workspaces": workspaces,
            }

        candidates = [html]

        for quoted in re.findall(r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)', html, re.S):
            try:
                decoded = json.loads(quoted)
            except Exception:
                continue
            if decoded:
                candidates.append(decoded)

        if '\\"' in html:
            candidates.append(html.replace('\\"', '"'))

        for candidate in candidates:
            parsed = _build_from_text(candidate)
            if parsed and parsed.get("workspaces"):
                return parsed

        return None

    # ══════════════════════════════════════════════════════════════════
    # 步骤 12：跟踪重定向链，交换 OAuth code → access_token
    # ══════════════════════════════════════════════════════════════════

    def follow_redirects_and_exchange_token(
        self, continue_url: str, oauth_start: OAuthStart
    ) -> dict:
        """跟踪重定向链，捕获 code= 回调 URL，交换 access_token。"""
        current_url = continue_url

        if current_url and "code=" in current_url and "state=" in current_url:
            self._log("起始 continue_url 已包含 code，直接交换 Token...")
            token_json = submit_callback_url(
                callback_url=current_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=oauth_start.redirect_uri,
                proxy_url=self.proxy,
            )
            return json.loads(token_json)

        for hop in range(8):
            resp = self.session.get(current_url, allow_redirects=False, timeout=15)
            location = resp.headers.get("Location") or ""
            self._log(
                f"follow[{hop + 1}] status={resp.status_code} "
                f"url={(current_url[:160] + '...') if len(current_url) > 160 else current_url} "
                f"location={(location[:160] + '...') if len(location) > 160 else (location or '(空)')}"
            )

            if resp.status_code not in (301, 302, 303, 307, 308) or not location:
                break

            next_url = urllib.parse.urljoin(current_url, location)
            self._log(f"重定向 [{hop + 1}] → {next_url[:100]}...")

            if "code=" in next_url and "state=" in next_url:
                self._log("捕获到 OAuth 回调 URL，交换 Token...")
                token_json = submit_callback_url(
                    callback_url=next_url,
                    expected_state=oauth_start.state,
                    code_verifier=oauth_start.code_verifier,
                    redirect_uri=oauth_start.redirect_uri,
                    proxy_url=self.proxy,
                )
                return json.loads(token_json)

            current_url = next_url

        raise RuntimeError("未能在重定向链中捕获到 OAuth 回调 URL（含 code= 参数）")
