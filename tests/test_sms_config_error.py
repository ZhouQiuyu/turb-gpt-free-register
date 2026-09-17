# -*- coding: utf-8 -*-
import sys
import unittest
from unittest.mock import patch, MagicMock

if "pyotp" not in sys.modules:
    sys.modules["pyotp"] = MagicMock()

from config import codex as codex_config
from core import sms_provider
from core import roxy_codex_oauth


class SmsConfigErrorTests(unittest.TestCase):
    def test_l_provider_empty_auth_code_raises_sms_config_error(self):
        with patch.object(codex_config, "SMS_PROVIDER", "l"), \
             patch.object(codex_config, "L_API_BASE", "http://127.0.0.1:8888"), \
             patch.object(codex_config, "L_ADMIN_AUTH_CODE", ""):
            http = MagicMock()
            with self.assertRaises(sms_provider.SmsConfigError) as ctx:
                sms_provider.acquire_number(http=http)
            self.assertIn("L_ADMIN_AUTH_CODE 不能为空", str(ctx.exception))

    def test_hero_provider_empty_key_raises_sms_config_error(self):
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", ""), \
             patch.object(codex_config, "SMS_API_KEY", ""):
            http = MagicMock()
            with self.assertRaises(sms_provider.SmsConfigError) as ctx:
                sms_provider.acquire_number(http=http)
            self.assertIn("Hero-SMS API key 不能为空", str(ctx.exception))

    def test_h_provider_empty_code_raises_sms_config_error(self):
        with patch.object(codex_config, "SMS_PROVIDER", "h"), \
             patch.object(codex_config, "H_API_BASE", "http://127.0.0.1:8788"), \
             patch.object(codex_config, "H_ADMIN_AUTH_CODE", ""):
            http = MagicMock()
            with self.assertRaises(sms_provider.SmsConfigError) as ctx:
                sms_provider.acquire_number(http=http)
            self.assertIn("H_ADMIN_AUTH_CODE 不能为空", str(ctx.exception))

    def test_roxy_oauth_aborts_immediately_on_sms_config_error(self):
        driver = MagicMock()
        with patch.object(roxy_codex_oauth, "_has_strict_add_phone_form", return_value=True), \
             patch.object(sms_provider, "acquire_number", side_effect=sms_provider.SmsConfigError("L_ADMIN_AUTH_CODE 不能为空")):
            with self.assertRaises(RuntimeError) as ctx:
                roxy_codex_oauth._do_phone_verification_if_present(driver)
            self.assertIn("已停止换号止损", str(ctx.exception))
            # Verify it only attempted once instead of 10 times
            self.assertEqual(sms_provider.acquire_number.call_count, 1)


if __name__ == "__main__":
    unittest.main()
