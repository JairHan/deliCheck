import hashlib
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import deli_eplus_auto_simple_v3 as app


class EnvFileTests(unittest.TestCase):
    def test_save_env_value_updates_once_and_locks_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DELI_MOBILE='13800138000'\nDELI_TRUST_CODE=''\n", encoding="utf-8")

            app.save_env_value(path, "DELI_TRUST_CODE", "new-trust-code")

            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("DELI_TRUST_CODE="), 1)
            self.assertIn('DELI_TRUST_CODE="new-trust-code"', text)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_load_env_file_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('DELI_TEST_VALUE="from-file"\n', encoding="utf-8")
            old_value = os.environ.get("DELI_TEST_VALUE")
            os.environ["DELI_TEST_VALUE"] = "from-system"
            try:
                app.load_env_file(path)
                self.assertEqual(os.environ["DELI_TEST_VALUE"], "from-system")
            finally:
                if old_value is None:
                    os.environ.pop("DELI_TEST_VALUE", None)
                else:
                    os.environ["DELI_TEST_VALUE"] = old_value

    def test_load_env_file_supports_smtp_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SMTP_SERVER='smtp.qq.com:465'\n", encoding="utf-8")
            old_value = os.environ.pop("SMTP_SERVER", None)
            try:
                app.load_env_file(path)
                self.assertEqual(os.environ["SMTP_SERVER"], "smtp.qq.com:465")
            finally:
                if old_value is None:
                    os.environ.pop("SMTP_SERVER", None)
                else:
                    os.environ["SMTP_SERVER"] = old_value


class EmailNotificationTests(unittest.TestCase):
    def test_smtp_endpoint_accepts_host_and_port(self):
        with patch.dict(
            os.environ,
            {"SMTP_SERVER": "smtp.qq.com:465", "SMTP_PORT": ""},
            clear=False,
        ):
            self.assertEqual(app.smtp_endpoint(True), ("smtp.qq.com", 465))

    def test_ssl_email_uses_authorization_code_and_defaults_to_sender(self):
        connection = MagicMock()
        smtp = connection.__enter__.return_value
        settings = {
            "SMTP_SSL": "true",
            "SMTP_EMAIL": "sender@qq.com",
            "SMTP_PASSWORD": "authorization-code",
            "SMTP_NAME": "青龙脚本运行通知",
            "SMTP_SERVER": "smtp.qq.com:465",
            "SMTP_PORT": "",
            "SMTP_TO": "",
        }

        with patch.dict(os.environ, settings, clear=False), patch.object(
            app.smtplib, "SMTP_SSL", return_value=connection
        ) as smtp_ssl:
            sent = app.send_email_notification("签到成功｜得力 E+", "测试日志")

        self.assertTrue(sent)
        smtp_ssl.assert_called_once_with("smtp.qq.com", 465, timeout=15.0)
        smtp.login.assert_called_once_with("sender@qq.com", "authorization-code")
        _, kwargs = smtp.send_message.call_args
        self.assertEqual(kwargs["from_addr"], "sender@qq.com")
        self.assertEqual(kwargs["to_addrs"], ["sender@qq.com"])
        message = smtp.send_message.call_args.args[0]
        self.assertIn("测试日志", message.get_content())
        self.assertEqual(str(message["Subject"]), "签到成功｜得力 E+")

    def test_push_prefers_reference_notify_interface(self):
        sender = Mock()
        with patch.object(app, "find_push_sender", return_value=("notify", sender)), patch.object(
            app, "send_email_notification"
        ) as smtp_fallback:
            channel = app.send_push_notification("签到失败｜得力 E+", "失败日志")

        self.assertEqual(channel, "notify")
        sender.assert_called_once_with("签到失败｜得力 E+", "失败日志")
        smtp_fallback.assert_not_called()


class BusinessOutcomeTests(unittest.TestCase):
    def make_deli(self):
        deli = Mock()
        deli.org_name = "测试组织"
        deli.ensure_terminal_id.return_value = ("terminal-id", False)
        deli.shift.return_value = {"workday": True, "has_scheduled": True}
        deli.action.return_value = "checkin"
        deli.records.return_value = []
        deli.validate_fixed_gps.return_value = {}
        deli.gps_proof.return_value = {"time": "123", "sig": "signature"}
        deli.execute.return_value = {"code": 0}
        return deli

    def test_real_checkin_success_is_visible_in_push_title(self):
        deli = self.make_deli()
        outcome = {}
        with patch.object(app, "Deli", return_value=deli), patch.object(
            app, "apply_random_delay"
        ), patch.object(app, "print_status"):
            app.run("check", execute=True, outcome=outcome)

        self.assertEqual(outcome["title"], "签到成功｜得力 E+")

    def test_failed_submission_is_visible_in_push_title(self):
        deli = self.make_deli()
        deli.execute.side_effect = app.DeliError("服务端拒绝")
        outcome = {}
        with patch.object(app, "Deli", return_value=deli), patch.object(
            app, "apply_random_delay"
        ), patch.object(app, "print_status"), self.assertRaises(app.DeliError):
            app.run("check", execute=True, outcome=outcome)

        self.assertEqual(outcome["title"], "签到失败｜得力 E+")

    def test_dry_run_title_says_no_submission(self):
        deli = self.make_deli()
        outcome = {}
        with patch.object(app, "Deli", return_value=deli), patch.object(
            app, "apply_random_delay"
        ), patch.object(app, "print_status"):
            app.run("check", execute=False, outcome=outcome)

        self.assertEqual(outcome["title"], "仅检查，未提交打卡｜得力 E+")


class RandomDelayTests(unittest.TestCase):
    def test_random_delay_uses_inclusive_configured_range(self):
        with patch.object(app.secrets, "randbelow", return_value=7) as randbelow, patch.object(
            app.time, "sleep"
        ) as sleep, patch.object(app.time, "time", return_value=1000), patch(
            "builtins.print"
        ) as output:
            delay = app.apply_random_delay("10")

        self.assertEqual(delay, 7)
        randbelow.assert_called_once_with(11)
        sleep.assert_called_once_with(7)
        expected_start = app.datetime.fromtimestamp(1007).strftime("%Y-%m-%d %H:%M:%S")
        output.assert_any_call("预计执行时间  :", expected_start)

    def test_zero_random_delay_does_not_sleep(self):
        with patch.object(app.secrets, "randbelow") as randbelow, patch.object(
            app.time, "sleep"
        ) as sleep:
            delay = app.apply_random_delay(0)

        self.assertEqual(delay, 0)
        randbelow.assert_not_called()
        sleep.assert_not_called()

    def test_random_delay_rejects_invalid_value(self):
        for value in ("abc", -1):
            with self.subTest(value=value), self.assertRaises(app.DeliError):
                app.apply_random_delay(value)


class SmsBootstrapTests(unittest.TestCase):
    def make_deli(self):
        deli = app.Deli()
        deli.cfg = dict(app.CONFIG)
        deli.cfg.update(
            {
                "mobile": "13800138000",
                "password": "test-password",
                "trust_code": "",
                "terminal_id": "",
            }
        )
        return deli

    def test_terminal_id_is_generated_once_and_persisted(self):
        deli = self.make_deli()
        fixed_uuid = uuid.UUID("12345678-1234-5678-9abc-def012345678")
        old_value = os.environ.get("DELI_TERMINAL_ID")

        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                app, "ENV_FILE", Path(directory) / ".env"
            ), patch.object(app.uuid, "uuid4", return_value=fixed_uuid) as uuid4:
                first, first_created = deli.ensure_terminal_id()
                second, second_created = deli.ensure_terminal_id()
                saved = app.ENV_FILE.read_text(encoding="utf-8")

            self.assertEqual(first, "12345678-1234-5678-9ABC-DEF012345678")
            self.assertEqual(second, first)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(uuid4.call_count, 1)
            self.assertIn(f'DELI_TERMINAL_ID="{first}"', saved)
        finally:
            if old_value is None:
                os.environ.pop("DELI_TERMINAL_ID", None)
            else:
                os.environ["DELI_TERMINAL_ID"] = old_value

    def test_explicit_terminal_id_is_never_overwritten(self):
        deli = self.make_deli()
        deli.cfg["terminal_id"] = "manual-terminal-id"

        with patch.object(app.uuid, "uuid4") as uuid4:
            terminal_id, created = deli.ensure_terminal_id()

        self.assertEqual(terminal_id, "manual-terminal-id")
        self.assertFalse(created)
        uuid4.assert_not_called()

    def test_send_login_sms_uses_official_signature_shape(self):
        deli = self.make_deli()
        deli.api = Mock(return_value={"code": 0})

        with patch.object(app.secrets, "choice", return_value="a"), patch.object(
            app.time, "time", return_value=1234.567
        ):
            deli.send_login_sms()

        _, url = deli.api.call_args.args
        kwargs = deli.api.call_args.kwargs
        self.assertTrue(url.endswith("/api/v3.0/auth/app/sms/send"))
        self.assertEqual(kwargs["json"]["nonce"], "aaaaaa")
        self.assertEqual(kwargs["json"]["timestamp"], "1234567")
        expected_raw = (
            "aaaaaa"
            + "13800138000"[::-1]
            + app.SMS_CODE_TYPE_LOGIN
            + "1234567"
            + app.SMS_SIGN_SECRET
        )
        expected_sign = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()
        self.assertEqual(kwargs["headers"]["sign"], expected_sign)

    def test_sms_login_saves_returned_trust_code(self):
        deli = self.make_deli()
        deli.api = Mock(
            return_value={
                "data": {
                    "trust_code": "returned-trust-code",
                    "token": "session-token",
                    "user_id": 123,
                }
            }
        )
        old_value = os.environ.get("DELI_TRUST_CODE")

        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                app, "ENV_FILE", Path(directory) / ".env"
            ):
                deli.sms_login_and_create_trust("123456")
                saved = app.ENV_FILE.read_text(encoding="utf-8")

            self.assertIn('DELI_TRUST_CODE="returned-trust-code"', saved)
            self.assertNotIn("123456", saved)
            self.assertEqual(deli.cfg["trust_code"], "returned-trust-code")
            self.assertEqual(deli.main_token, "session-token")
            self.assertEqual(deli.user_id, "123")
        finally:
            if old_value is None:
                os.environ.pop("DELI_TRUST_CODE", None)
            else:
                os.environ["DELI_TRUST_CODE"] = old_value

    def test_noninteractive_bootstrap_does_not_send_sms(self):
        deli = self.make_deli()
        deli.send_login_sms = Mock()
        old_code = os.environ.pop("DELI_SMS_CODE", None)

        try:
            with patch.object(app.sys.stdin, "isatty", return_value=False):
                with self.assertRaises(app.DeliError):
                    deli.bootstrap_trust_code()
            deli.send_login_sms.assert_not_called()
        finally:
            if old_code is not None:
                os.environ["DELI_SMS_CODE"] = old_code


if __name__ == "__main__":
    unittest.main()
