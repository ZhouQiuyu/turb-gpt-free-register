# -*- coding: utf-8 -*-
import json
import pytest
from core import card_binding_service, db
from webui.app import create_app


def test_luhn_checksum():
    # Valid Visa card sample (4000123456789017 has valid Luhn checksum)
    assert card_binding_service.luhn_checksum("4000123456789017") is True
    assert card_binding_service.luhn_checksum("4532015112830366") is True
    # Invalid card
    assert card_binding_service.luhn_checksum("4000123456789011") is False
    assert card_binding_service.luhn_checksum("") is False
    assert card_binding_service.luhn_checksum("123") is False


def test_identify_card_brand():
    assert card_binding_service.identify_card_brand("4000123456789010") == "Visa"
    assert card_binding_service.identify_card_brand("5105105105105100") == "Mastercard"
    assert card_binding_service.identify_card_brand("378282246310005") == "American Express"
    assert card_binding_service.identify_card_brand("6011111111111117") == "Discover"
    assert card_binding_service.identify_card_brand("6221260000000000") == "Discover"
    assert card_binding_service.identify_card_brand("6200000000000000") == "UnionPay"


def test_parse_card_input_pipe_format():
    raw = "4000123456789010|12|28|123"
    res = card_binding_service.parse_card_input(raw)
    assert res["valid"] is True
    assert res["card_number"] == "4000123456789010"
    assert res["last4"] == "9010"
    assert res["exp_month"] == "12"
    assert res["exp_year"] == "2028"
    assert res["cvc"] == "123"
    assert res["brand"] == "Visa"
    assert res["country"] == "US"


def test_parse_card_input_slash_format():
    raw = "4000123456789010/05/2029/987/97201"
    res = card_binding_service.parse_card_input(raw)
    assert res["valid"] is True
    assert res["card_number"] == "4000123456789010"
    assert res["exp_month"] == "05"
    assert res["exp_year"] == "2029"
    assert res["cvc"] == "987"
    assert res["postal_code"] == "97201"


def test_parse_card_input_text_format():
    raw = "Card: 5105105105105100 Exp: 03/27 CVC: 456"
    res = card_binding_service.parse_card_input(raw)
    assert res["valid"] is True
    assert res["card_number"] == "5105105105105100"
    assert res["last4"] == "5100"
    assert res["exp_month"] == "03"
    assert res["exp_year"] == "2027"
    assert res["cvc"] == "456"
    assert res["brand"] == "Mastercard"


def test_parse_card_input_merchant_hyphen_and_full_profile():
    raw = "4859540179366553----2030/6----383----NIKKI BRYANT----2182 E 78th St,Chicago 60649,US"
    res = card_binding_service.parse_card_input(raw)
    assert res["valid"] is True
    assert res["card_number"] == "4859540179366553"
    assert res["last4"] == "6553"
    assert res["exp_month"] == "06"
    assert res["exp_year"] == "2030"
    assert res["cvc"] == "383"
    assert res["brand"] == "Visa"
    assert res["cardholder_name"] == "NIKKI BRYANT"
    assert res["postal_code"] == "60649"
    assert "Chicago" in res["raw_address"]

    # When generating billing with this card info and name
    billing = card_binding_service.generate_tax_free_billing(
        country="US",
        hint_zip=res["postal_code"],
        name=res["cardholder_name"],
    )
    assert billing["name"] == "NIKKI BRYANT"
    # Chicago 60649 is not tax-free, so it automatically fell back to a real tax-free state
    assert billing["state"] in ("OR", "DE", "MT", "NH", "AK")


def test_parse_card_input_separated_year_month():
    raw = "4859540179366553----2030----06----383"
    res = card_binding_service.parse_card_input(raw)
    assert res["valid"] is True
    assert res["card_number"] == "4859540179366553"
    assert res["exp_month"] == "06"
    assert res["exp_year"] == "2030"
    assert res["cvc"] == "383"


def test_parse_card_input_invalid():
    res = card_binding_service.parse_card_input("invalid string")
    assert res["valid"] is False
    assert res["error"] is not None


def test_generate_tax_free_billing():
    # Random selection
    b1 = card_binding_service.generate_tax_free_billing(country="US")
    assert b1["country"] == "US"
    assert b1["state"] in ("OR", "DE", "MT", "NH", "AK")
    assert bool(b1["city"])
    assert bool(b1["postal_code"])
    assert bool(b1["line1"])
    assert bool(b1["name"])

    # Matching zip
    b2 = card_binding_service.generate_tax_free_billing(country="US", hint_zip="97201")
    assert b2["postal_code"] == "97201"
    assert b2["state"] == "OR"
    assert b2["city"] == "Portland"


def test_db_card_binding_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", tmp_path / "accounts.json")
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)

    acc_id = 99999
    db._save_accounts([{
        "id": acc_id,
        "email": "test_bind@example.com",
        "plan_type": "free",
        "access_token": "mock_token",
    }])

    # Mark running
    ok = db.mark_account_card_binding_running(acc_id)
    assert ok is True
    acc = db.get_account(acc_id)
    assert acc["card_binding_status"] == "running"
    assert acc["card_binding_message"] == "绑卡任务运行中"

    # Recover interrupted
    recovered = db.recover_interrupted_card_bindings()
    assert recovered == 1
    acc = db.get_account(acc_id)
    assert acc["card_binding_status"] == "failed"
    assert "重启" in acc["card_binding_error"]

    # Update success
    db.update_account_card_binding(acc_id, {
        "ok": True,
        "status": "success",
        "message": "绑卡成功",
        "card_brand": "Visa",
        "card_last4": "9010",
    })
    acc = db.get_account(acc_id)
    assert acc["card_binding_status"] == "success"
    assert acc["card_binding_ok"] is True
    assert acc["card_brand"] == "Visa"
    assert acc["card_last4"] == "9010"
    assert acc["plan_type"] == "plus"
    assert acc["current_plan_type"] == "plus"


def test_db_proxy_by_country(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", tmp_path / "accounts.json")
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)

    db.create_proxy({
        "protocol": "socks5h",
        "host": "1.2.3.4",
        "port": 1080,
        "status": "active",
    })
    db.create_proxy({
        "protocol": "socks5h",
        "host": "5.6.7.8",
        "port": 1080,
        "status": "active",
    })
    # Update country code
    with db._LOCK, db.closing(db._sqlite_conn()) as conn:
        conn.execute("UPDATE proxy_pool SET country_code='US', country='United States' WHERE host='1.2.3.4'")
        conn.execute("UPDATE proxy_pool SET country_code='JP', country='Japan' WHERE host='5.6.7.8'")
        conn.commit()

    us_proxies = db.get_active_proxies_by_country("US")
    assert len(us_proxies) == 1
    assert "1.2.3.4" in us_proxies[0]["host"]

    picked_us = db.pick_proxy_by_country("US")
    assert "1.2.3.4:1080" in picked_us


def test_webui_parse_card_endpoint():
    app = create_app(auth_code="test-auth")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    resp = client.post("/api/accounts/parse-card", json={"card_text": "4000123456789010|12|28|123"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["card"]["brand"] == "Visa"
    assert data["card"]["last4"] == "9010"
    assert data["billing"]["state"] in ("OR", "DE", "MT", "NH", "AK")

    # Invalid card
    err_resp = client.post("/api/accounts/parse-card", json={"card_text": "bad_input"})
    assert err_resp.status_code == 400
    err_data = err_resp.get_json()
    assert err_data["ok"] is False


def test_webui_bind_card_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_ACCOUNTS_JSON", tmp_path / "accounts.json")
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)

    acc_id = 8888
    db._save_accounts([{
        "id": acc_id,
        "email": "carduser@example.com",
        "plan_type": "free",
        "plus_trial_eligible": True,
        "access_token": "valid_token_123",
    }])

    # Mock native extract checkout URL
    monkeypatch.setattr(
        card_binding_service,
        "extract_native_checkout_url",
        lambda *args, **kwargs: {
            "ok": True,
            "already_paid": False,
            "url": "https://checkout.stripe.com/c/pay/cs_live_mock123",
            "checkout_session_id": "cs_live_mock123",
        }
    )

    app = create_app(auth_code="test-auth")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    # 1. Mode: link_only
    resp = client.post("/api/accounts/bind-card", json={
        "account_id": acc_id,
        "card_text": "4000123456789010|12|28|123",
        "mode": "link_only",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["mode"] == "link_only"
    assert "https://checkout.stripe.com" in data["url"]

    # 2. Mode: auto
    # Mock bind_card_with_cloak
    monkeypatch.setattr(
        card_binding_service,
        "bind_card_with_cloak",
        lambda acc_id, card_info, proxy_url=None, log_cb=None: {
            "ok": True,
            "status": "success",
            "message": "开通 Plus 成功",
        }
    )

    resp_auto = client.post("/api/accounts/bind-card", json={
        "account_id": acc_id,
        "card_text": "4000123456789010|12|28|123",
        "mode": "auto",
    })
    assert resp_auto.status_code == 200
    auto_data = resp_auto.get_json()
    assert auto_data["ok"] is True
    job_id = auto_data["job_id"]
    assert job_id.startswith("bind_8888_")

    # 3. Check status
    import time
    time.sleep(0.2)
    resp_status = client.get(f"/api/accounts/bind-card/status/{job_id}")
    assert resp_status.status_code == 200
    status_data = resp_status.get_json()
    assert status_data["ok"] is True
    assert status_data["job"]["status"] in ("running", "success")

