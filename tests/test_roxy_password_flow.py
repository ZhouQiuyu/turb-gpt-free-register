# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import roxy_registration as roxy


class RoxyPasswordFlowTests(unittest.TestCase):
    def test_is_email_verification_page_excludes_password_pages(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/create-account/password"
        self.assertFalse(roxy._is_email_verification_page(driver))

        driver.current_url = "https://auth.openai.com/signup/password"
        self.assertFalse(roxy._is_email_verification_page(driver))

        driver.current_url = "https://auth.openai.com/log-in/password"
        self.assertFalse(roxy._is_email_verification_page(driver))

        driver.current_url = "https://auth.openai.com/email-verification?client_id=123"
        self.assertTrue(roxy._is_email_verification_page(driver))

    def test_fill_password_does_not_exit_prematurely_after_clicking_continue(self):
        driver = MagicMock()
        calls = {"otp_page": 0, "click_btn": 0}

        def fake_is_email_verif(drv):
            calls["otp_page"] += 1
            return calls["otp_page"] <= 2

        def fake_click_continue(drv):
            calls["click_btn"] += 1
            if calls["click_btn"] == 1:
                return {"ok": True, "reason": "clicked_continue_with_password"}
            return {"ok": False, "reason": "missing_continue_with_password"}

        with patch.object(roxy, "_is_email_verification_page", side_effect=fake_is_email_verif), \
             patch.object(roxy, "_click_continue_with_password_if_present", side_effect=fake_click_continue), \
             patch.object(roxy, "_is_signup_password_page", return_value=True), \
             patch.object(roxy, "_is_login_password_page", return_value=False), \
             patch.object(roxy, "_has_access_token", return_value=False), \
             patch.object(roxy, "_password_page_state", return_value={"inputs": [], "buttons": []}), \
             patch.object(roxy, "_registration_password", return_value="TestPass123!"), \
             patch.object(roxy, "_human_type_text"), \
             patch.object(roxy, "human_delay"), \
             patch.object(roxy, "_human_click"):
            fake_input = MagicMock()
            fake_button = MagicMock()
            driver.execute_script.side_effect = [
                {"ok": True, "input": fake_input, "button": fake_button},  # targets
                {"ok": True, "button": fake_button},  # submit_result
            ]
            password = roxy._fill_password_page_if_present(driver, "test@example.com", timeout=10)
            self.assertEqual(password, "TestPass123!")
            self.assertEqual(calls["click_btn"], 1)

    def test_type_otp_heals_when_on_signup_password_page(self):
        driver = MagicMock()
        state = {"on_password": True}

        def fake_is_signup_pwd(drv):
            return state["on_password"]

        def fake_click_passwordless(drv):
            state["on_password"] = False
            return {"ok": True}

        fake_input = MagicMock()
        fake_input.is_displayed.return_value = True
        fake_input.get_attribute.side_effect = lambda k: "one-time-code" if k == "autocomplete" else ""

        with patch.object(roxy, "_is_signup_password_page", side_effect=fake_is_signup_pwd), \
             patch.object(roxy, "_click_passwordless_signup_if_present", side_effect=fake_click_passwordless), \
             patch.object(roxy, "_visible", return_value=True), \
             patch.object(roxy, "_human_type_text") as mock_type:
            driver.find_elements.side_effect = lambda by, sel: [fake_input] if not state["on_password"] else []
            roxy._type_otp(driver, "123456", timeout=5)
            mock_type.assert_called_once_with(driver, fake_input, "123456", clear=True)
