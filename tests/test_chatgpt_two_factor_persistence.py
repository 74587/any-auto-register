"""TOTP 密钥从 enroll 响应一路走到数据库。

密钥只在 enroll 响应里下发一次，中途任何一环把它丢了这个号的 2FA 就废了，
所以注册链和补绑链两条路都要有落库断言，而不是只测到内存里的 result。
"""

import json
import unittest
from unittest import mock

from sqlmodel import Session, delete, select

from core.base_platform import Account, AccountStatus
from core.db import AccountModel, engine, save_account
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.protocol.auth_flow import AuthResult
from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

SECRET = "JBSWY3DPEHPK3PXP"


class _FakeFlow:
    """只保留注册引擎会碰到的那几个面：结果对象和 session_ready 钩子。"""

    def __init__(self, *_args, on_session_ready=None, raise_after_hook=None, **_kwargs):
        self.result = AuthResult()
        self.result.email = "demo@example.com"
        self.result.password = "pw-demo"
        self.result.access_token = "at-1"
        self.result.session_token = "sess-1"
        self._on_session_ready = on_session_ready
        self._raise_after_hook = raise_after_hook

    def run_register(self, _mail_provider):
        if self._on_session_ready is not None:
            self._on_session_ready(self, self.result.access_token)
        if self._raise_after_hook is not None:
            raise self._raise_after_hook
        return self.result


def _run_engine(*, raise_after_hook=None, bind_result=None):
    engine_under_test = ChatGPTRegistrationEngine(
        mailbox=mock.Mock(), email="demo@example.com", bind_2fa=True, log_fn=lambda _m: None
    )
    bind_result = bind_result or TwoFactorBindResult(ok=True, secret=SECRET)

    def _flow_factory(*args, **kwargs):
        return _FakeFlow(*args, raise_after_hook=raise_after_hook, **kwargs)

    with mock.patch(
        "platforms.chatgpt.registration_engine.AuthFlow", side_effect=_flow_factory
    ), mock.patch(
        "platforms.chatgpt.registration_engine.resolve_sms_settings", return_value={}
    ), mock.patch(
        "platforms.chatgpt.registration_engine.build_phone_callback", return_value=None
    ), mock.patch.object(
        ChatGPTRegistrationEngine, "_build_mail_provider", return_value=mock.Mock()
    ), mock.patch(
        "platforms.chatgpt.registration_engine.bind_totp_inline", return_value=bind_result
    ), mock.patch(
        "platforms.chatgpt.registration_engine.bind_totp_via_login",
        return_value=TwoFactorBindResult(error_message="重登未果"),
    ) as slow_path:
        return engine_under_test.run(), slow_path


class RegisterPathPersistenceTests(unittest.TestCase):
    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()

    def test_inline_bind_puts_the_secret_in_the_registration_metadata(self):
        result, slow_path = _run_engine()

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["totp_secret"], SECRET)
        self.assertTrue(result.metadata["chatgpt_2fa"]["bound"])
        # 快路径已经拿到密钥，不该再去跑一次带 PoW 的重登
        slow_path.assert_not_called()

    def test_secret_survives_the_hand_off_to_the_account_record(self):
        result, _ = _run_engine()
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.extra["totp_secret"], SECRET)

    def test_secret_is_written_to_the_database(self):
        result, _ = _run_engine()
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})

        save_account(adapter.build_account(result, fallback_password="fallback"))

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "demo@example.com")
            ).first()
        self.assertEqual(json.loads(row.extra_json)["totp_secret"], SECRET)

    def test_a_late_crash_does_not_lose_an_already_bound_secret(self):
        # 典型场景：2FA 绑完了，后面 Codex 换 refresh_token 那步炸了
        result, _ = _run_engine(raise_after_hook=RuntimeError("codex oauth 失败"))

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["totp_secret"], SECRET)
        self.assertTrue(result.metadata["chatgpt_2fa"]["bound"])

    def test_a_failed_bind_leaves_no_secret_but_records_the_attempt(self):
        result, slow_path = _run_engine(
            bind_result=TwoFactorBindResult(error_message="enroll 403")
        )

        slow_path.assert_called_once()
        self.assertFalse(result.metadata["chatgpt_2fa"]["bound"])
        # 没绑上时账号 extra 里干脆没有这个键，账号页才不会显示成"已绑"
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})
        account = adapter.build_account(result, fallback_password="fallback")
        self.assertNotIn("totp_secret", account.extra)


class BindActionPersistenceTests(unittest.TestCase):
    """老号补绑走的是 action 的 ``account_extra_patch`` 通道。"""

    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            model = AccountModel(platform="chatgpt", email="old@example.com", password="pw")
            model.set_extra({"access_token": "at-1"})
            session.add(model)
            session.commit()
            self.account_id = model.id

    def _apply(self, result: dict) -> dict:
        from api.actions import _apply_action_result

        with Session(engine) as session:
            row = session.get(AccountModel, self.account_id)
            _apply_action_result("chatgpt", "bind_2fa", row, result, session)
            session.commit()
            session.refresh(row)
            return row.get_extra()

    def test_successful_bind_persists_the_secret_next_to_the_credentials(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
        from services import chatgpt_two_factor

        account = Account(
            platform="chatgpt",
            email="old@example.com",
            password="pw",
            token="at-1",
            status=AccountStatus.REGISTERED,
            extra={"access_token": "at-1"},
        )
        with mock.patch.object(
            chatgpt_two_factor,
            "bind_account_two_factor",
            return_value=TwoFactorBindResult(ok=True, secret=SECRET),
        ):
            result = ChatGPTPlatform().execute_action("bind_2fa", account, {})

        extra = self._apply(result)
        self.assertEqual(extra["totp_secret"], SECRET)
        self.assertTrue(extra["chatgpt_2fa"]["bound"])
        self.assertEqual(extra["access_token"], "at-1")

    def test_a_failed_bind_never_clears_a_secret_that_is_already_there(self):
        from services.chatgpt_two_factor import build_extra_patch
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        with Session(engine) as session:
            row = session.get(AccountModel, self.account_id)
            row.set_extra({"access_token": "at-1", "totp_secret": SECRET})
            session.add(row)
            session.commit()

        extra = self._apply(
            {"ok": False, "account_extra_patch": build_extra_patch(TwoFactorBindResult(error_message="enroll 403"))}
        )

        self.assertEqual(extra["totp_secret"], SECRET)
        self.assertFalse(extra["chatgpt_2fa"]["bound"])


if __name__ == "__main__":
    unittest.main()
