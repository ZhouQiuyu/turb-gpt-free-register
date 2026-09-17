# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path

from webui import config_editor


class SmsCountryComboboxTests(unittest.TestCase):
    def test_editable_fields_have_country_select_widget(self):
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        
        self.assertIn("SMS_COUNTRY", fields)
        self.assertEqual(fields["SMS_COUNTRY"].get("widget"), "country_select")
        self.assertEqual(fields["SMS_COUNTRY"].get("help"), "选择或搜索国家代码；留空则不限国家")

        self.assertIn("HERO_SMS_COUNTRY", fields)
        self.assertEqual(fields["HERO_SMS_COUNTRY"].get("widget"), "country_select")
        self.assertEqual(fields["HERO_SMS_COUNTRY"].get("help"), "选择或搜索 Hero-SMS 专属国家代码；留空则复用通用国家代码")

    def test_countries_json_validity(self):
        countries_path = Path(__file__).resolve().parent.parent / "webui" / "countries.json"
        self.assertTrue(countries_path.exists(), "countries.json must exist")
        
        with open(countries_path, "r", encoding="utf-8") as f:
            countries = json.load(f)
            
        self.assertGreaterEqual(len(countries), 190)
        codes = [c["code"] for c in countries]
        self.assertIn("187", codes)  # US
        self.assertIn("4", codes)    # PH
        self.assertIn("16", codes)   # GB

    def test_templates_contain_combobox_code(self):
        templates_dir = Path(__file__).resolve().parent.parent / "webui" / "templates"
        for tpl_name in ["index.html", "index_legacy.html"]:
            tpl_path = templates_dir / tpl_name
            self.assertTrue(tpl_path.exists(), f"{tpl_name} must exist")
            content = tpl_path.read_text(encoding="utf-8")

            self.assertIn(".country-combobox", content, f"{tpl_name} must include combobox CSS")
            self.assertIn("SMS_COUNTRIES", content, f"{tpl_name} must include SMS_COUNTRIES constant")
            self.assertIn("renderCountryCombobox", content, f"{tpl_name} must include renderCountryCombobox")
            self.assertIn("readConfigElementValue", content, f"{tpl_name} must include readConfigElementValue")
            self.assertIn("country_select", content, f"{tpl_name} must check widget === 'country_select'")
            self.assertIn("country-combobox-input", content, f"{tpl_name} must handle country-combobox-input")
