import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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


class SmsBootstrapTests(unittest.TestCase):
    def make_deli(self):
        deli = app.Deli()
        deli.cfg = dict(app.CONFIG)
        deli.cfg.update(
            {
                "mobile": "13800138000",
                "password": "test-password",
                "trust_code": "",
            }
        )
        return deli

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
