# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config
from config import env_loader
from webui import config_editor


class _Resp:
    def __init__(self, text, status_code=200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("No JSON")


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class HeroSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields_include_hero(self):
        self.assertIn("HERO_SMS_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("HERO_SMS_API_KEY", fields)
        self.assertIn("HERO_SMS_API_BASE", fields)
        self.assertIn("HERO_SMS_SERVICE", fields)
        self.assertIn("HERO_SMS_COUNTRY", fields)
        self.assertIn("HERO_SMS_MAX_PRICE", fields)
        self.assertTrue(fields["HERO_SMS_API_KEY"].get("secret"))
        self.assertEqual(fields["HERO_SMS_API_KEY"].get("storage"), "env")

    def test_acquire_number_hero_success(self):
        http = _Http([_Resp("ACCESS_NUMBER:998877:12025550123")])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_hero_key"), \
             patch.object(codex_config, "HERO_SMS_SERVICE", "dr"), \
             patch.object(codex_config, "HERO_SMS_COUNTRY", "187"), \
             patch.object(codex_config, "HERO_SMS_MAX_PRICE", "0.5"):
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "998877")
        self.assertEqual(phone, "12025550123")
        self.assertEqual(len(http.calls), 1)
        params = http.calls[0]["params"]
        self.assertEqual(params["action"], "getNumber")
        self.assertEqual(params["api_key"], "test_hero_key")
        self.assertEqual(params["service"], "dr")
        self.assertEqual(params["country"], "187")
        self.assertEqual(params["maxPrice"], "0.5")

    def test_wait_for_sms_code_hero_success(self):
        http = _Http([
            _Resp("STATUS_WAIT_CODE"),
            _Resp("STATUS_OK:654321"),
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_hero_key"):
            code = sms_provider.wait_for_sms_code("998877", http=http, max_wait=5, poll_interval=0)

        self.assertEqual(code, "654321")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[0]["params"]["id"], "998877")

    def test_cancel_hero_immediate_release(self):
        http = _Http([_Resp("ACCESS_CANCEL")])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_hero_key"):
            sms_provider.cancel("998877", http=http, background=False)

        self.assertEqual(len(http.calls), 1)
        params = http.calls[0]["params"]
        self.assertEqual(params["action"], "setStatus")
        self.assertEqual(params["status"], "8")
        self.assertEqual(params["id"], "998877")

    def test_complete_hero(self):
        http = _Http([_Resp("ACCESS_ACTIVATION")])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_hero_key"):
            sms_provider.complete("998877", http=http)

        self.assertEqual(len(http.calls), 1)
        params = http.calls[0]["params"]
        self.assertEqual(params["action"], "setStatus")
        self.assertEqual(params["status"], "6")
        self.assertEqual(params["id"], "998877")

    def test_hero_error_handling(self):
        # 1. BAD_KEY (text)
        http = _Http([_Resp("BAD_KEY", status_code=200)])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "bad_key"):
            with self.assertRaises(sms_provider.SmsProviderError) as ctx:
                sms_provider.acquire_number(http=http)
            self.assertIn("BAD_KEY", str(ctx.exception))

        # 2. NO_BALANCE (text)
        http = _Http([_Resp("NO_BALANCE", status_code=200)])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_key"):
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider.acquire_number(http=http)

        # 3. NO_NUMBERS (text)
        http = _Http([_Resp("NO_NUMBERS", status_code=200)])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_key"):
            with self.assertRaises(sms_provider.SmsNoNumbersError):
                sms_provider.acquire_number(http=http)

        # 4. JSON 401 error
        http = _Http([_Resp('{"title":"BAD_KEY","details":"Unauthorized"}', status_code=401, json_data={"title": "BAD_KEY", "details": "Unauthorized"})])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "bad_key"):
            with self.assertRaises(sms_provider.SmsProviderError) as ctx:
                sms_provider.acquire_number(http=http)
            self.assertIn("BAD_KEY", str(ctx.exception))

        # 5. JSON 422 NO_BALANCE
        http = _Http([_Resp('{"title":"NO_BALANCE","details":"Low balance"}', status_code=422, json_data={"title": "NO_BALANCE", "details": "Low balance"})])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), \
             patch.object(codex_config, "HERO_SMS_API_KEY", "test_key"):
            with self.assertRaises(sms_provider.SmsNoBalanceError):
                sms_provider.acquire_number(http=http)


if __name__ == "__main__":
    unittest.main()
