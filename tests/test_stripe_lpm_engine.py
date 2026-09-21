# -*- coding: utf-8 -*-
"""
Stripe LPM 协议提链引擎单元测试。
验证 iDEAL、PIX、UPI、Kakao Pay 12步协议握手、状态迁移及资产解析。
"""
import unittest
from unittest.mock import MagicMock, patch

from core.stripe_lpm_engine import LPM_SPECS, StripeLPMExtractor


class TestStripeLPMEngine(unittest.TestCase):
    def setUp(self):
        self.dummy_session = "cs_live_a1b2c3d4e5f6g7h8i9j0"
        self.dummy_key = "pk_live_51M6DummyStripePublicKey123456"

    def test_init_and_url_parsing(self):
        url = f"https://checkout.stripe.com/c/pay/{self.dummy_session}#apiKey={self.dummy_key}"
        extractor = StripeLPMExtractor(url, target_lpm="ideal")
        self.assertEqual(extractor.session_id, self.dummy_session)
        self.assertEqual(extractor.api_key, self.dummy_key)
        self.assertEqual(extractor.target_lpm, "ideal")
        self.assertEqual(extractor.spec["country"], "NL")

    def test_init_with_explicit_api_key(self):
        extractor = StripeLPMExtractor(self.dummy_session, target_lpm="pix", api_key=self.dummy_key)
        self.assertEqual(extractor.session_id, self.dummy_session)
        self.assertEqual(extractor.api_key, self.dummy_key)
        self.assertEqual(extractor.target_lpm, "pix")
        self.assertEqual(extractor.spec["country"], "BR")

    @patch("core.stripe_lpm_engine.curl_requests", None)
    def test_ideal_full_flow(self):
        extractor = StripeLPMExtractor(self.dummy_session, target_lpm="ideal", api_key=self.dummy_key)

        mock_session = MagicMock()
        extractor.session = mock_session

        # Mock /init response
        mock_init_resp = MagicMock()
        mock_init_resp.status_code = 200
        mock_init_resp.json.return_value = {
            "account_id": "acct_1M6xxxOpenAI",
            "expected_amount": 2000,
            "eid": "NA",
            "payment_method_types": ["card", "ideal", "pix"],
        }

        # Mock /update_taxes response (NL 21% VAT)
        mock_tax_resp = MagicMock()
        mock_tax_resp.status_code = 200
        mock_tax_resp.json.return_value = {
            "snapshot": {"amount_total": 2420},
        }

        # Mock /confirm response
        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 200
        mock_confirm_resp.json.return_value = {
            "payment_intent": {
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {
                        "url": "https://hooks.stripe.com/redirect/authenticate/src_12345?client_secret=src_client_secret_67890",
                        "return_url": "https://chatgpt.com/api/auth/session",
                    },
                },
            },
        }

        mock_session.post.side_effect = [mock_init_resp, mock_tax_resp, mock_confirm_resp]

        result = extractor.run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "ideal")
        self.assertEqual(result["url"], "https://hooks.stripe.com/redirect/authenticate/src_12345?client_secret=src_client_secret_67890")
        self.assertEqual(extractor.expected_amount, 2420)
        self.assertEqual(mock_session.post.call_count, 3)

        # 校验调用 URL 均以 api.stripe.com 为前缀
        for call_args in mock_session.post.call_args_list:
            called_url = call_args[0][0]
            self.assertTrue(called_url.startswith("https://api.stripe.com/v1/payment_pages/"))

    @patch("core.stripe_lpm_engine.curl_requests", None)
    def test_pix_full_flow(self):
        extractor = StripeLPMExtractor(self.dummy_session, target_lpm="pix", api_key=self.dummy_key)

        mock_session = MagicMock()
        extractor.session = mock_session

        mock_init_resp = MagicMock()
        mock_init_resp.status_code = 200
        mock_init_resp.json.return_value = {
            "account_id": "acct_1M6xxxOpenAI",
            "expected_amount": 2000,
        }

        mock_tax_resp = MagicMock()
        mock_tax_resp.status_code = 200
        mock_tax_resp.json.return_value = {
            "snapshot": {"amount_total": 2000},
        }

        mock_confirm_resp = MagicMock()
        mock_confirm_resp.status_code = 200
        mock_confirm_resp.json.return_value = {
            "payment_intent": {
                "next_action": {
                    "type": "display_pix_qr_code",
                    "display_pix_qr_code": {
                        "hosted_instructions_url": "https://payments.stripe.com/pix/instructions/test_voucher_url",
                        "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...",
                        "copia_e_cola": "00020101021226830014br.gov.bcb.pix2561stripe.com/pix/...",
                        "expires_at": 1789999999,
                    },
                },
            },
        }

        mock_session.post.side_effect = [mock_init_resp, mock_tax_resp, mock_confirm_resp]

        result = extractor.run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "pix")
        self.assertEqual(result["url"], "https://payments.stripe.com/pix/instructions/test_voucher_url")
        self.assertEqual(result["copy_paste"], "00020101021226830014br.gov.bcb.pix2561stripe.com/pix/...")
        self.assertTrue(result["qr_code"].startswith("data:image/png;base64,"))
        self.assertEqual(result["expires_at"], 1789999999)

    @patch("core.stripe_lpm_engine.curl_requests", None)
    def test_kakao_pay_pre_confirm(self):
        extractor = StripeLPMExtractor(self.dummy_session, target_lpm="kakao_pay", api_key=self.dummy_key)

        mock_session = MagicMock()
        extractor.session = mock_session

        mock_init = MagicMock(status_code=200)
        mock_init.json.return_value = {"expected_amount": 2000}

        mock_tax = MagicMock(status_code=200)
        mock_tax.json.return_value = {"snapshot": {"amount_total": 2000}}

        mock_pre = MagicMock(status_code=200)
        mock_pre.json.return_value = {"status": "requires_payment_method"}

        mock_confirm = MagicMock(status_code=200)
        mock_confirm.json.return_value = {
            "payment_intent": {
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://online-pay.kakao.com/mock_pay"},
                }
            }
        }

        mock_session.post.side_effect = [mock_init, mock_tax, mock_pre, mock_confirm]

        result = extractor.run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "kakao_pay")
        self.assertEqual(result["url"], "https://online-pay.kakao.com/mock_pay")
        # Kakao Pay 必须调用 4 次 post (包括 pre_confirm)
        self.assertEqual(mock_session.post.call_count, 4)


if __name__ == "__main__":
    unittest.main()
