"""手机号注册链路与绑定邮箱链路。

两条链都要花钱：号是租的、邮箱是池里领的。所以这里盯的主要是"什么时候该换号、
什么时候该收手、绑定失败会不会把已经注册好的号一起判死"。
"""

import json
import unittest
from unittest import mock

from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTER_FLOW_EMAIL,
    CHATGPT_REGISTER_FLOW_PHONE,
    CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    normalize_chatgpt_register_flow,
    resolve_chatgpt_register_flow,
)
from platforms.chatgpt.protocol.auth_flow import AuthFlow, AuthResult
from platforms.chatgpt.registration_engine import (
    REGISTER_FLOW_PHONE,
    REGISTER_FLOW_PHONE_WITH_EMAIL,
    ChatGPTRegistrationEngine,
    RegistrationResult,
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        if not self._responses:
            raise AssertionError(f"没有为 {url} 准备响应")
        return self._responses.pop(0)


def _flow(responses=()) -> AuthFlow:
    """绕开 AuthFlow.__init__（要建 http 客户端），只装被测方法用到的东西。"""
    flow = AuthFlow.__new__(AuthFlow)
    flow.session = _FakeSession(responses)
    flow.result = AuthResult()
    flow.result.device_id = ""
    flow._last_sentinel_token = ""
    flow._last_sentinel_so_token = ""
    flow._on_password = None
    flow._bind_email_error = ""
    flow._common_headers = lambda referer="": {"Referer": referer}
    flow._trace_http = lambda *args, **kwargs: None
    flow._get_env = lambda key, default="": default
    return flow


class _FakeMailProvider:
    def __init__(self, email="pool@example.com", code="123456"):
        self.email = email
        self.code = code
        self.created = 0
        self.waits = []

    def create_mailbox(self):
        self.created += 1
        return self.email

    def wait_for_otp(self, email, timeout=120, issued_after=None):
        self.waits.append((email, timeout, issued_after))
        return self.code


class PhoneIdentityRequestTests(unittest.TestCase):
    def test_authorize_continue_submits_phone_kind(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "create_account_password"}})])

        flow.phone_authorize_continue("+56971901026", "sentinel-token")

        call = flow.session.calls[0]
        self.assertEqual(
            call["url"], "https://auth.openai.com/api/accounts/authorize/continue"
        )
        self.assertEqual(
            call["json"]["username"],
            {"value": "+56971901026", "kind": "phone_number"},
        )
        self.assertEqual(call["headers"]["Referer"], "https://auth.openai.com/log-in?usernameKind=phone_number")
        self.assertEqual(call["headers"]["openai-sentinel-token"], "sentinel-token")

    def test_email_authorize_continue_keeps_email_kind(self):
        flow = _flow([_FakeResponse(payload={})])

        flow.authorize_continue("demo@example.com", "sentinel-token")

        self.assertEqual(
            flow.session.calls[0]["json"]["username"],
            {"value": "demo@example.com", "kind": "email"},
        )

    def test_register_user_uses_phone_as_username_and_persists_password(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "phone_otp_verification"}})])
        saved = []
        flow._on_password = lambda identity, password: saved.append((identity, password))

        flow.phone_register_user("+56971901026")

        call = flow.session.calls[0]
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/user/register")
        self.assertEqual(call["json"]["username"], "+56971901026")
        self.assertTrue(call["json"]["password"])
        self.assertEqual(saved, [("+56971901026", flow.result.password)])

    def test_register_user_raises_on_non_200(self):
        flow = _flow([_FakeResponse(status_code=400, text="invalid_username")])

        with self.assertRaises(RuntimeError) as ctx:
            flow.phone_register_user("+56971901026")

        self.assertIn("invalid_username", str(ctx.exception))


class BindEmailTests(unittest.TestCase):
    def test_add_email_send_posts_email(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "email_otp_verification"}})])

        flow.add_email_send("UraLepre287@outlook.com")

        call = flow.session.calls[0]
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/add-email/send")
        self.assertEqual(call["json"], {"email": "UraLepre287@outlook.com"})
        self.assertEqual(call["headers"]["Referer"], "https://auth.openai.com/add-email")
        # 这一步真实浏览器不带 sentinel，带了反而是风控特征
        self.assertNotIn("openai-sentinel-token", call["headers"])

    def test_add_email_send_surfaces_server_message(self):
        flow = _flow([
            _FakeResponse(status_code=400, payload={"error": {"message": "email_already_in_use"}})
        ])

        with self.assertRaises(RuntimeError) as ctx:
            flow.add_email_send("taken@example.com")

        self.assertIn("email_already_in_use", str(ctx.exception))

    def test_bind_email_sends_code_then_validates(self):
        flow = _flow([
            _FakeResponse(payload={"page": {"type": "email_otp_verification"}}),
            _FakeResponse(payload={"continue_url": "https://auth.openai.com/authorize/continue?x=1"}),
        ])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        next_url = flow.bind_email(provider)

        self.assertEqual(provider.created, 1)
        self.assertEqual(flow.result.bound_email, "pool@example.com")
        self.assertEqual(
            [call["url"] for call in flow.session.calls],
            [
                "https://auth.openai.com/api/accounts/add-email/send",
                "https://auth.openai.com/api/accounts/email-otp/validate",
            ],
        )
        self.assertEqual(flow.session.calls[1]["json"], {"code": "123456"})
        self.assertEqual(next_url, "https://auth.openai.com/authorize/continue?x=1")

    def test_bind_email_resends_once_when_code_is_rejected(self):
        flow = _flow([
            _FakeResponse(payload={}),
            _FakeResponse(status_code=401, text="invalid code"),
            _FakeResponse(payload={}),
            _FakeResponse(payload={"continue_url": "https://auth.openai.com/next"}),
        ])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        flow.bind_email(provider)

        self.assertEqual(
            [call["url"].rsplit("/", 2)[-2:] for call in flow.session.calls],
            [
                ["add-email", "send"],
                ["email-otp", "validate"],
                ["add-email", "send"],
                ["email-otp", "validate"],
            ],
        )
        self.assertEqual(flow.result.bound_email, "pool@example.com")

    def test_bind_failure_keeps_the_account(self):
        flow = _flow([_FakeResponse(status_code=400, payload={"error": {"message": "invalid state"}})])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        result = flow._try_bind_email(provider, "https://auth.openai.com/next")

        self.assertEqual(result, "https://auth.openai.com/next")
        self.assertFalse(flow.result.bound_email)
        self.assertIn("invalid state", flow._bind_email_error)

    def test_no_provider_means_no_binding(self):
        flow = _flow()

        self.assertEqual(flow._try_bind_email(None, "url"), "url")


class FlowStateDetectionTests(unittest.TestCase):
    def test_add_email_state(self):
        self.assertTrue(AuthFlow._is_add_email_state(page_type="add_email"))
        self.assertTrue(
            AuthFlow._is_add_email_state(continue_url="https://auth.openai.com/add-email")
        )
        self.assertFalse(AuthFlow._is_add_email_state(page_type="about_you"))

    def test_phone_otp_state(self):
        self.assertTrue(AuthFlow._is_phone_otp_state(page_type="phone_otp_verification"))
        self.assertTrue(
            AuthFlow._is_phone_otp_state(continue_url="https://auth.openai.com/phone-verification")
        )
        self.assertFalse(AuthFlow._is_phone_otp_state(page_type="email_otp_verification"))

    def test_existing_identity_state(self):
        self.assertTrue(AuthFlow._is_existing_identity_state(page_type="login_password"))
        self.assertTrue(
            AuthFlow._is_existing_identity_state(continue_url="https://auth.openai.com/log-in/password")
        )
        self.assertFalse(
            AuthFlow._is_existing_identity_state(page_type="create_account_password")
        )


class _FakeController:
    provider_key = "smsbower"

    def __init__(self, max_attempts=3):
        self.config = {
            "sms_per_phone_timeout": "40",
            "sms_max_phone_attempts": str(max_attempts),
            "sms_code_retries_per_phone": "1",
        }
        self.rented = []
        self.refunds = []
        self.cleanups = 0
        self.successes = 0

    def set_resend_callback(self, callback):
        pass

    def get_phone(self):
        phone = f"+5697190{len(self.rented):04d}"
        self.rented.append(phone)
        return phone

    def get_code(self, timeout=0):
        return "654321"

    def mark_send_failed(self, reason=""):
        self.refunds.append(reason)

    def mark_send_succeeded(self):
        pass

    def mark_code_failed(self, reason=""):
        pass

    def report_success(self):
        self.successes += 1

    def cleanup(self):
        self.cleanups += 1


def _loop_flow() -> AuthFlow:
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow._get_env = lambda key, default="": default
    flow.get_csrf_token = lambda: "csrf"
    flow.get_auth_url = lambda csrf, login_hint="": "https://auth.openai.com/authorize"
    flow.auth_oauth_init = lambda url: "device-1"
    flow.get_sentinel_token = lambda device_id: "sentinel"
    flow._normalize_continue_url = lambda url: url
    return flow


class PhoneRegisterLoopTests(unittest.TestCase):
    def test_already_registered_number_moves_to_the_next_one(self):
        ctrl = _FakeController(max_attempts=3)
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "login_password"}
        }

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)
        self.assertEqual(len(ctrl.refunds), 3)

    def test_broken_flow_state_stops_burning_numbers(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _loop_flow()

        def _continue(phone, sentinel):
            raise RuntimeError("Invalid authorization step.")

        flow.phone_authorize_continue = _continue

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 1)

    def test_same_unknown_error_three_times_stops_the_round(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _loop_flow()

        def _continue(phone, sentinel):
            raise RuntimeError("upstream hiccup")

        flow.phone_authorize_continue = _continue

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)

    def test_happy_path_registers_then_validates_sms(self):
        ctrl = _FakeController()
        flow = _loop_flow()
        registered = []
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }

        def _register(phone):
            registered.append(phone)
            return {"page": {"type": "phone_otp_verification"}}

        flow.phone_register_user = _register
        flow._phone_otp_validate = lambda code: {"continue_url": "https://auth.openai.com/about-you"}

        continue_url, auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(registered, [ctrl.rented[0]])
        self.assertEqual(continue_url, "https://auth.openai.com/about-you")
        self.assertEqual(auth_url, "https://auth.openai.com/authorize")
        self.assertEqual(flow.result.phone_number, ctrl.rented[0])
        self.assertEqual(ctrl.successes, 1)

    def test_server_skipping_the_password_page_still_triggers_the_sms(self):
        ctrl = _FakeController()
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {"page": {"type": "about_you"}}
        sent = []

        def _add_phone_send(phone):
            sent.append(phone)
            return {"page": {"type": "phone_otp_verification"}}

        flow._add_phone_send = _add_phone_send
        flow._phone_otp_validate = lambda code: {"continue_url": "https://auth.openai.com/about-you"}

        flow._do_phone_register_loop(ctrl)

        self.assertEqual(sent, [ctrl.rented[0]])


class RegistrationEngineFlowDispatchTests(unittest.TestCase):
    def _engine(self, register_flow, sms_callback=object()):
        engine = ChatGPTRegistrationEngine(
            mailbox=object(),
            proxy=None,
            extra_config={},
            log_fn=lambda _message: None,
            register_flow=register_flow,
        )
        engine._build_sms_callback = lambda: sms_callback
        return engine

    def test_phone_flow_does_not_claim_a_mailbox(self):
        engine = self._engine(REGISTER_FLOW_PHONE)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = ""

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        flow.run_phone_register.assert_called_once_with(mail_provider=None, bind_email=False)
        self.assertTrue(result.success)
        self.assertEqual(result.email, "+56971901026")
        self.assertEqual(result.metadata["phone_number"], "+56971901026")
        self.assertEqual(result.source, "phone_register")

    def test_phone_with_email_flow_passes_a_mail_provider(self):
        engine = self._engine(REGISTER_FLOW_PHONE_WITH_EMAIL)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.bound_email = "pool@example.com"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = ""

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        kwargs = flow.run_phone_register.call_args.kwargs
        self.assertTrue(kwargs["bind_email"])
        self.assertIsNotNone(kwargs["mail_provider"])
        # 绑上邮箱之后账号按邮箱记账，手机号留在 metadata 里
        self.assertEqual(result.email, "pool@example.com")
        self.assertEqual(result.metadata["bound_email"], "pool@example.com")
        self.assertEqual(result.metadata["phone_number"], "+56971901026")

    def test_unbound_email_is_reported_without_failing_the_account(self):
        engine = self._engine(REGISTER_FLOW_PHONE_WITH_EMAIL)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = "invalid state"

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.email, "+56971901026")
        self.assertEqual(result.metadata["bind_email_error"], "invalid state")

    def test_phone_flow_without_sms_fails_with_a_clear_message(self):
        engine = self._engine(REGISTER_FLOW_PHONE, sms_callback=None)

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("接码", result.error_message)


class RegisterFlowResolutionTests(unittest.TestCase):
    def test_defaults_to_email(self):
        self.assertEqual(resolve_chatgpt_register_flow({}), CHATGPT_REGISTER_FLOW_EMAIL)
        self.assertEqual(normalize_chatgpt_register_flow("nonsense"), CHATGPT_REGISTER_FLOW_EMAIL)

    def test_accepts_phone_variants(self):
        self.assertEqual(normalize_chatgpt_register_flow("phone"), CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(normalize_chatgpt_register_flow("SMS"), CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(
            normalize_chatgpt_register_flow("phone-with-email"),
            CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
        )
        self.assertEqual(
            normalize_chatgpt_register_flow("phone_bind_email"),
            CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
        )

    def test_adapter_reads_flow_from_task_extra(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_register_flow": "phone_with_email"}
        )
        self.assertEqual(adapter.register_flow, CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL)

    def test_account_extra_records_flow_and_identities(self):
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_register_flow": "phone"})
        result = RegistrationResult(
            success=True,
            email="+56971901026",
            password="pw",
            access_token="at",
            session_token="st",
            source="phone_register",
            metadata={"phone_number": "+56971901026", "bound_email": ""},
        )

        account = adapter.build_account(result, "fallback")

        self.assertEqual(account.email, "+56971901026")
        self.assertEqual(account.extra["chatgpt_register_flow"], CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(account.extra["phone_number"], "+56971901026")
        self.assertNotIn("bound_email", account.extra)

    def test_engine_receives_the_flow_from_the_adapter(self):
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_register_flow": "phone"})
        context = ChatGPTRegistrationContext(
            mailbox=object(),
            proxy_url=None,
            callback_logger=lambda _message: None,
            email="",
            password="pw",
            extra_config={},
        )

        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine"
        ) as engine_cls:
            engine_cls.return_value.run.return_value = RegistrationResult(success=True)
            adapter.run(context)

        self.assertEqual(
            engine_cls.call_args.kwargs["register_flow"], CHATGPT_REGISTER_FLOW_PHONE
        )


if __name__ == "__main__":
    unittest.main()
