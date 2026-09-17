# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import mailnest_client


class MailNestClientTests(unittest.TestCase):
    def setUp(self):
        mailnest_client._CONTEXT_CACHE.clear()

    def test_pick_account_requires_api_key(self):
        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "", create=True):
            with self.assertRaisesRegex(mailnest_client.MailNestClientError, "MailNest API Key"):
                mailnest_client.pick_account()

    @patch("core.mailnest_client.requests.request")
    def test_pick_account_buys_mailbox_with_project_code(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"code": "00000", "data": [{"email": "fresh@mailnest.test"}]}
        request.return_value = response

        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            mailnest_client._email_cfg, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            account = mailnest_client.pick_account()

        self.assertEqual(account.email, "fresh@mailnest.test")
        self.assertIs(mailnest_client.get_account_context("fresh@mailnest.test"), account)
        request.assert_called_once()
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer key-123")
        self.assertEqual(kwargs["json"], {"project_code": "chatgpt001", "count": 1})
        self.assertEqual(account.sale_mode, "temporary")

    @patch("core.mailnest_client.requests.request")
    def test_pick_account_buys_exclusive_mailbox(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": "00000",
            "data": [{"email": "exclusive@mailnest.test", "sale_mode": "exclusive"}],
        }
        request.return_value = response

        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            mailnest_client._email_cfg, "MAIL_NEST_MODE", "exclusive", create=True
        ):
            account = mailnest_client.pick_account()

        self.assertEqual(account.email, "exclusive@mailnest.test")
        self.assertEqual(account.sale_mode, "exclusive")
        self.assertEqual(account.project_code, "")
        request.assert_called_once()
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer key-123")
        self.assertEqual(kwargs["json"], {"count": 1})
        self.assertTrue(request.call_args[0][1].endswith("/api/v1/email/exclusive/buy"))

    @patch("core.mailnest_client.requests.request")
    def test_release_account_calls_release_api_on_failure(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"code": "00000", "data": None}
        request.return_value = response

        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "key-123", create=True):
            mailnest_client._CONTEXT_CACHE["fail@mailnest.test"] = mailnest_client.MailNestAccount(email="fail@mailnest.test")
            mailnest_client.release_account("fail@mailnest.test", status="failed")

        self.assertNotIn("fail@mailnest.test", mailnest_client._CONTEXT_CACHE)
        request.assert_called_once()
        self.assertTrue(request.call_args[0][1].endswith("/api/v1/email/release"))
        self.assertEqual(request.call_args.kwargs["json"], {"email": "fail@mailnest.test"})

    @patch("core.mailnest_client.requests.request")
    def test_release_account_does_not_call_api_when_used(self, request):
        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "key-123", create=True):
            mailnest_client._CONTEXT_CACHE["ok@mailnest.test"] = mailnest_client.MailNestAccount(email="ok@mailnest.test")
            mailnest_client.release_account("ok@mailnest.test", status="used")

        self.assertNotIn("ok@mailnest.test", mailnest_client._CONTEXT_CACHE)
        request.assert_not_called()

    @patch("core.mailnest_client.time.sleep")
    @patch("core.mailnest_client.requests.request")
    def test_fetch_latest_otp_reads_code_match(self, request, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": "00000",
            "data": [{"code_match": "654321", "subject": "OpenAI code", "from": "noreply@openai.com", "timestamp": 205}],
        }
        request.return_value = response

        with patch.object(mailnest_client._email_cfg, "MAIL_NEST_API_KEY", "key-123", create=True):
            code = mailnest_client.fetch_latest_otp(
                "fresh@mailnest.test",
                after_ts=200,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")


if __name__ == "__main__":
    unittest.main()
