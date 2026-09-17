# -*- coding: utf-8 -*-
import sys
import unittest
from unittest.mock import MagicMock

if "flask" not in sys.modules:
    sys.modules["flask"] = MagicMock()
if "pyotp" not in sys.modules:
    sys.modules["pyotp"] = MagicMock()

from webui.app import _compact_account_for_list


class PlusPromoFieldsTests(unittest.TestCase):
    def test_compact_account_for_list_preserves_plus_promo_details(self):
        row = {
            "id": 101,
            "email": "test@example.com",
            "plan_type": "free",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_discount_percentage": 50,
            "plus_trial_duration_num_periods": 3,
            "plus_trial_duration_period": "month",
            "plus_trial_title": "Get a 50% discount on Plus for 3 months",
            "plus_trial_campaign_id": "promo-123",
        }
        cleaned = _compact_account_for_list(row)
        self.assertEqual(cleaned.get("plus_trial_eligible"), True)
        self.assertEqual(cleaned.get("plus_trial_discount_percentage"), 50)
        self.assertEqual(cleaned.get("plus_trial_duration_num_periods"), 3)
        self.assertEqual(cleaned.get("plus_trial_duration_period"), "month")
        self.assertEqual(cleaned.get("plus_trial_title"), "Get a 50% discount on Plus for 3 months")
        self.assertEqual(cleaned.get("plus_trial_campaign_id"), "promo-123")


if __name__ == "__main__":
    unittest.main()
