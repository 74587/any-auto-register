import unittest
from unittest.mock import patch

from api.tasks import (
    DEFAULT_REGISTER_RETRY_TIMES,
    MAX_REGISTER_RETRY_TIMES,
    RegisterTaskRequest,
    _create_task_record,
    _run_register,
    _task_store,
    normalize_register_retry_times,
)
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, BasePlatform
from core.task_runtime import NonRetryableRegisterError


class _FakeMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        def poll_once():
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=0.01,
            poll_once=poll_once,
        )


class _FakePlatform(BasePlatform):
    name = "fake"
    display_name = "Fake"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        account = self.mailbox.get_email()
        self.mailbox.wait_for_code(account, timeout=1)
        return Account(
            platform="fake",
            email=account.email,
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    _counter = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def reset_counter(cls):
        cls._counter = 0

    def register(self, email: str, password: str = None) -> Account:
        type(self)._counter += 1
        index = type(self)._counter
        return Account(
            platform="chatgpt",
            email=f"user{index}@example.com",
            password=password or "pw",
            extra={"workspace_id": f"ws-{index}"},
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FlakyPlatform(BasePlatform):
    """前 ``fail_times`` 轮直接抛错，之后正常返回账号。"""

    name = "chatgpt"
    display_name = "ChatGPT"

    fail_times = 1
    attempts = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def reset(cls, fail_times: int):
        cls.fail_times = fail_times
        cls.attempts = 0

    def register(self, email: str, password: str = None) -> Account:
        type(self).attempts += 1
        if type(self).attempts <= type(self).fail_times:
            raise RuntimeError(f"第 {type(self).attempts} 轮炸了")
        return Account(
            platform="chatgpt",
            email=f"retried{type(self).attempts}@example.com",
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class RegisterRetryRoundsTests(unittest.TestCase):
    """整流程重试：一次失败不该直接把这个序号判死。"""

    def _run(self, task_id: str, *, fail_times: int, retry_times: int):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            register_retry_times=retry_times,
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)
        _FlakyPlatform.reset(fail_times)
        saved_logs: list[tuple] = []

        with (
            patch("core.registry.get", return_value=_FlakyPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch(
                "api.tasks._save_task_log",
                side_effect=lambda *args, **kwargs: saved_logs.append((args, kwargs)),
            ),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id), saved_logs

    def test_failed_round_is_retried_from_scratch(self):
        snapshot, saved_logs = self._run("task-retry-recovers", fail_times=1, retry_times=1)
        joined = "\n".join(snapshot["logs"])

        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(_FlakyPlatform.attempts, 2)
        self.assertIn("重开第 2/2 轮", joined)
        self.assertIn("开始注册第 1/1 个账号（第 2/2 轮）", joined)
        # 中途失败不该在注册记录里留下一条 failed，否则统计会双记
        self.assertEqual([args[2] for args, _ in saved_logs], ["success"])

    def test_retries_are_capped_and_the_last_failure_is_recorded(self):
        snapshot, saved_logs = self._run("task-retry-exhausted", fail_times=5, retry_times=2)

        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(_FlakyPlatform.attempts, 3)
        self.assertEqual([args[2] for args, _ in saved_logs], ["failed"])

    def test_zero_retries_keeps_the_old_single_round_behaviour(self):
        snapshot, _saved = self._run("task-retry-disabled", fail_times=5, retry_times=0)
        joined = "\n".join(snapshot["logs"])

        self.assertEqual(_FlakyPlatform.attempts, 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertNotIn("[RETRY]", joined)
        # 只有一轮时日志不该多出"第 x/y 轮"的噪声
        self.assertIn("开始注册第 1/1 个账号\n", joined + "\n")


class _DeadEndPlatform(_FlakyPlatform):
    """每轮都以"重开也没用"的方式失败。"""

    attempts = 0

    def register(self, email: str, password: str = None) -> Account:
        type(self).attempts += 1
        raise NonRetryableRegisterError("手机号 +2349157587437 的账号已在 OpenAI 侧创建")


class NonRetryableFailureTests(unittest.TestCase):
    """号源被静默拦下时，多开几轮只会多几个孤号。"""

    def test_dead_end_failure_skips_the_remaining_rounds(self):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=1,
            concurrency=1,
            register_retry_times=4,
            extra={"mail_provider": "fake"},
        )
        _create_task_record("task-retry-dead-end", req, "manual", None)
        _DeadEndPlatform.attempts = 0
        saved_logs: list[tuple] = []

        with (
            patch("core.registry.get", return_value=_DeadEndPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch(
                "api.tasks._save_task_log",
                side_effect=lambda *args, **kwargs: saved_logs.append((args, kwargs)),
            ),
        ):
            _run_register("task-retry-dead-end", req)

        snapshot = _task_store.snapshot("task-retry-dead-end")
        joined = "\n".join(snapshot["logs"])

        self.assertEqual(_DeadEndPlatform.attempts, 1)
        self.assertIn("跳过剩下 4 轮", joined)
        self.assertEqual(len(snapshot["errors"]), 1)
        # 提前收手也要落一条 failed 记录，否则这个序号在统计里凭空消失
        self.assertEqual([args[2] for args, _ in saved_logs], ["failed"])


class RegisterRetryTimesNormalisationTests(unittest.TestCase):
    def test_blank_and_garbage_fall_back_to_the_default(self):
        for value in ("", None, "abc", "  "):
            self.assertEqual(normalize_register_retry_times(value), DEFAULT_REGISTER_RETRY_TIMES)

    def test_zero_is_honoured_and_absurd_values_are_capped(self):
        self.assertEqual(normalize_register_retry_times(0), 0)
        self.assertEqual(normalize_register_retry_times("3"), 3)
        self.assertEqual(normalize_register_retry_times(-4), 0)
        self.assertEqual(normalize_register_retry_times(999), MAX_REGISTER_RETRY_TIMES)


class RegisterTaskControlFlowTests(unittest.TestCase):
    def _build_request(self, **overrides):
        payload = {
            "platform": "fake",
            "count": 1,
            "concurrency": 1,
            "proxy": "http://proxy.local:8080",
            "extra": {"mail_provider": "fake"},
        }
        payload.update(overrides)
        return RegisterTaskRequest(**payload)

    def _run_with_control(self, task_id: str, *, stop: bool = False, skip: bool = False):
        req = self._build_request()
        _create_task_record(task_id, req, "manual", None)
        if stop:
            _task_store.request_stop(task_id)
        if skip:
            _task_store.request_skip_current(task_id)

        with (
            patch("core.registry.get", return_value=_FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_skip_current_marks_attempt_as_skipped(self):
        snapshot = self._run_with_control("task-control-skip", skip=True)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_stop_marks_task_as_stopped(self):
        snapshot = self._run_with_control("task-control-stop", stop=True)

        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["errors"], [])

    def test_successful_run_logs_progress_for_each_account(self):
        task_id = "task-control-progress"
        req = self._build_request(platform="chatgpt", count=2, concurrency=1)
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTPlatform.reset_counter()

        with (
            patch("core.registry.get", return_value=_FakeChatGPTPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        joined_logs = "\n".join(snapshot["logs"])

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 2)
        self.assertIn("开始注册第 1/2 个账号", joined_logs)
        self.assertIn("注册成功: user1@example.com", joined_logs)
        self.assertIn("注册成功: user2@example.com", joined_logs)


if __name__ == "__main__":
    unittest.main()
