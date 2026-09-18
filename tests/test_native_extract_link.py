# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core import geo_utils
from core import db
from core import proxy_service
from core import extract_link_service


class TestNativeExtractLink(unittest.TestCase):
    @staticmethod
    def storage(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "agent",
            "_LEGACY_CODEX_EXPORT_STATE": root / "state.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }

    def test_geo_utils_flag_and_badge(self):
        self.assertEqual(geo_utils.get_country_flag("JP"), "🇯🇵")
        self.assertEqual(geo_utils.get_country_flag("US"), "🇺🇸")
        self.assertEqual(geo_utils.get_country_flag(""), "🌐")
        self.assertEqual(geo_utils.get_country_flag("UNKNOWN"), "🌐")

        self.assertEqual(geo_utils.get_country_cn_name("JP"), "日本")
        self.assertEqual(geo_utils.get_country_cn_name("US"), "美国")
        self.assertEqual(geo_utils.get_country_cn_name("XYZ", fallback="某国"), "某国")

        badge_jp = geo_utils.format_country_badge("JP")
        self.assertIn("🇯🇵", badge_jp)
        self.assertIn("日本", badge_jp)
        self.assertIn("(JP)", badge_jp)

        badge_un = geo_utils.format_country_badge("")
        self.assertIn("未知", badge_un)
        self.assertIn("(UN)", badge_un)

        info = geo_utils.get_country_badge_info("SG")
        self.assertEqual(info["code"], "SG")
        self.assertEqual(info["flag"], "🇸🇬")
        self.assertEqual(info["name_cn"], "新加坡")
        self.assertIn("🇸🇬 新加坡 (SG)", info["badge"])

    def test_strict_proxy_picking(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db._ensure_sqlite()
            with db.closing(db._sqlite_conn()) as conn:
                conn.execute("DELETE FROM proxy_pool")
                conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_proxy_import_completed', '1')")
                conn.commit()

            jp_proxy = db.create_proxy({
                "name": "JP Node",
                "protocol": "socks5h",
                "host": "1.2.3.4",
                "port": 1080,
                "status": "active",
            })
            db.update_proxy(
                jp_proxy["id"],
                {
                    "latency_ms": 80,
                    "exit_ip": "1.2.3.4",
                    "country_code": "JP",
                    "country": "Japan",
                    "city": "Tokyo",
                    "status": "active",
                }
            )

            us_proxy = db.create_proxy({
                "name": "US Node",
                "protocol": "socks5h",
                "host": "5.6.7.8",
                "port": 1080,
                "status": "active",
            })
            db.update_proxy(
                us_proxy["id"],
                {
                    "latency_ms": 150,
                    "exit_ip": "5.6.7.8",
                    "country_code": "US",
                    "country": "United States",
                    "city": "New York",
                    "status": "active",
                }
            )

            # 1. Matching JP in strict mode
            picked_jp = db.pick_proxy_by_country("JP", strict=True)
            self.assertEqual(picked_jp, "socks5h://1.2.3.4:1080")

            # 2. Matching US in strict mode
            picked_us = db.pick_proxy_by_country("US", strict=True)
            self.assertEqual(picked_us, "socks5h://5.6.7.8:1080")

            # 3. Requesting KR in strict mode (no KR proxy) -> MUST return empty string ""
            picked_kr_strict = db.pick_proxy_by_country("KR", strict=True)
            self.assertEqual(picked_kr_strict, "")

            # 4. Requesting KR in non-strict mode -> falls back to available active proxy
            picked_kr_loose = db.pick_proxy_by_country("KR", strict=False)
            self.assertIn(picked_kr_loose, ("socks5h://1.2.3.4:1080", "socks5h://5.6.7.8:1080"))

    def test_account_inherits_proxy_country(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db._ensure_sqlite()
            with db.closing(db._sqlite_conn()) as conn:
                conn.execute("DELETE FROM proxy_pool")
                conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_proxy_import_completed', '1')")
                conn.commit()

            proxy_url = "socks5h://10.0.0.1:1080"
            node = db.create_proxy({
                "name": "UK Node",
                "protocol": "socks5h",
                "host": "10.0.0.1",
                "port": 1080,
                "status": "active",
            })
            db.update_proxy(
                node["id"],
                {
                    "latency_ms": 100,
                    "exit_ip": "10.0.0.1",
                    "country_code": "GB",
                    "country": "United Kingdom",
                    "city": "London",
                    "status": "active",
                }
            )

            acc_id = db.insert_account(
                email="test_uk@example.com",
                access_token="at_xxx",
                proxy_used=proxy_url
            )

            account = db.get_account(acc_id)
            self.assertIsNotNone(account)
            self.assertEqual(account["country_code"], "GB")
            self.assertEqual(account["country"], "United Kingdom")
            self.assertEqual(account["city"], "London")
            self.assertIn("🇬🇧", account["country_badge"])
            self.assertIn("英国", account["country_badge"])

    def test_proxy_auto_activation(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db._ensure_sqlite()
            with db.closing(db._sqlite_conn()) as conn:
                conn.execute("DELETE FROM proxy_pool")
                conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_proxy_import_completed', '1')")
                conn.commit()

            node = db.create_proxy({
                "name": "Test Node",
                "protocol": "socks5h",
                "host": "192.168.1.100",
                "port": 1080,
            })
            p = db.get_proxy(node["id"])
            self.assertEqual(p["status"], "disabled")

            db.update_proxy(
                node["id"],
                {
                    "latency_ms": 120,
                    "exit_ip": "150.1.2.3",
                    "country_code": "JP",
                    "country": "Japan",
                    "city": "Tokyo",
                    "status": "active",
                }
            )

            updated = db.get_proxy(node["id"])
            self.assertEqual(updated["status"], "active")
            self.assertEqual(updated["country_code"], "JP")
            self.assertIn("🇯🇵", updated["country_badge"])

    def test_extract_link_skips_when_no_geo_proxy(self):
        with tempfile.TemporaryDirectory() as td, patch.multiple(db, **self.storage(Path(td))):
            db._ensure_sqlite()
            with db.closing(db._sqlite_conn()) as conn:
                conn.execute("DELETE FROM proxy_pool")
                conn.execute("INSERT OR REPLACE INTO storage_meta(key, value) VALUES('legacy_proxy_import_completed', '1')")
                conn.commit()

            acc_id = db.insert_account(
                email="tokyo_user@example.com",
                access_token="at_xxx",
                country_code="JP",
                country="Japan"
            )

            us_node = db.create_proxy({
                "name": "US Node Only",
                "protocol": "socks5h",
                "host": "1.1.1.1",
                "port": 1080,
            })
            db.update_proxy(
                us_node["id"],
                {
                    "latency_ms": 90,
                    "exit_ip": "1.1.1.1",
                    "country_code": "US",
                    "country": "United States",
                    "city": "Ashburn",
                    "status": "active",
                }
            )

            extract_link_service._run_extract(account_id=acc_id)

            updated_acc = db.get_account(acc_id)
            self.assertEqual(updated_acc["extract_link_status"], "skipped")
            self.assertIn("JP", updated_acc["extract_link_error"])
            self.assertIn("暂无该属地活跃代理", updated_acc["extract_link_error"])
            self.assertIn("已自动跳过提链", updated_acc["extract_link_error"])


if __name__ == "__main__":
    unittest.main()
