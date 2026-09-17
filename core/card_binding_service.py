# -*- coding: utf-8 -*-
"""
一键自动绑卡与原生提链核心服务。
支持：
1. 卡密多格式智能切分解析 (卡号/月/年/CVV/邮编/地址) 与 BIN 发卡国识别。
2. 美国五大免税州 (OR/DE/MT/NH/AK) 真实合规账单生成 (0 消费税)。
3. 原生 Stripe Checkout 官方直链生成 (零外部商业依赖，替代 CDK)。
4. 基于 CloakBrowser 与分段美区住宅代理的全自动无头绑卡引擎。
5. 异步后台任务队列与账号权益自动同步。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from core import db

logger = logging.getLogger(__name__)

_BINDING_WORKERS = 3
_BINDING_EXECUTOR = ThreadPoolExecutor(max_workers=_BINDING_WORKERS, thread_name_prefix="card-binding")
_BINDING_JOBS_LOCK = threading.Lock()
_BINDING_JOBS: dict[str, dict] = {}


# ==============================================================================
# 1. 智能卡密解析与 BIN 识别
# ==============================================================================

def luhn_checksum(card_number: str) -> bool:
    """标准 Luhn 算法校验信用卡卡号合法性。"""
    clean = re.sub(r"\D", "", str(card_number or ""))
    if len(clean) < 13 or len(clean) > 19:
        return False
    digits = [int(d) for d in clean]
    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = digit * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += digit
    return checksum % 10 == 0


def identify_card_brand(card_number: str) -> str:
    """根据卡头识别信用卡品牌。"""
    c = re.sub(r"\D", "", str(card_number or ""))
    if c.startswith("4"):
        return "Visa"
    if re.match(r"^(5[1-5]|2[2-7])", c):
        return "Mastercard"
    if re.match(r"^(34|37)", c):
        return "American Express"
    if re.match(r"^(6011|65|64[4-9]|622)", c):
        return "Discover"
    if re.match(r"^(352[89]|35[3-8][0-9])", c):
        return "JCB"
    if c.startswith("62"):
        return "UnionPay"
    return "Unknown"


def identify_card_country(card_number: str) -> tuple[str, str]:
    """识别发卡国代码 (ISO 2字码) 与国家全名。
    网上购买的 0 刀试用卡绝大多数为美卡 (US)，此处默认美卡并支持智能扩展。
    """
    brand = identify_card_brand(card_number)
    if brand == "UnionPay":
        return "CN", "China"
    return "US", "United States"


def parse_card_input(raw_text: str) -> dict[str, Any]:
    """
    智能解析任意常见卡密输入格式。
    支持格式：
      - 4000123456789010|12|28|123
      - 4000123456789010 12/28 123
      - 4000123456789010/12/2028/123/97201
      - 带描述文本：Card: 4000... Exp: 12/28 CVV: 123
    返回归一化字典：
      {
        "card_number": "4000123456789010",
        "last4": "9010",
        "exp_month": "12",
        "exp_year": "2028",
        "cvc": "123",
        "brand": "Visa",
        "country": "US",
        "postal_code": "97201" (若输入附带),
        "valid": True/False,
        "error": None | str
      }
    """
    text = str(raw_text or "").strip()
    if not text:
        return {"valid": False, "error": "卡密内容为空"}

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    first_line = lines[0] if lines else text

    card_match = re.search(r"\b([3-6]\d{12,18})\b", text)
    if not card_match:
        no_space = re.sub(r"[\s-]", "", first_line)
        card_match = re.search(r"([3-6]\d{12,18})", no_space)

    if not card_match:
        return {"valid": False, "error": "未识别到有效的 13~19 位信用卡卡号"}

    card_number = card_match.group(1)
    last4 = card_number[-4:]
    brand = identify_card_brand(card_number)
    country_code, country_name = identify_card_country(card_number)

    remaining = text.replace(card_number, " ", 1)

    exp_month = ""
    exp_year = ""
    cvc = ""
    hint_zip = ""

    # 1. 查找 MM/YY 或 MM/YYYY 格式
    exp_match = re.search(r"\b(0[1-9]|1[0-2])\s*[/|-]\s*(20\d{2}|\d{2})\b", remaining)
    if exp_match:
        exp_month = exp_match.group(1)
        raw_year = exp_match.group(2)
        exp_year = f"20{raw_year}" if len(raw_year) == 2 else raw_year
        remaining = remaining.replace(exp_match.group(0), " ", 1)
    else:
        parts = [p.strip() for p in re.split(r"[|/,\s]+", first_line) if p.strip()]
        token_parts = [p for p in parts if p != card_number]
        if len(token_parts) >= 3:
            p0, p1, p2 = token_parts[0], token_parts[1], token_parts[2]
            if p0.isdigit() and 1 <= int(p0) <= 12 and p1.isdigit():
                exp_month = f"{int(p0):02d}"
                exp_year = f"20{p1}" if len(p1) == 2 else p1
                cvc = p2
                remaining = ""
                if len(token_parts) >= 4 and token_parts[3].isdigit() and len(token_parts[3]) == 5:
                    hint_zip = token_parts[3]

    # 2. 提取 CVC
    if not cvc:
        cvc_match = re.search(r"\b(\d{3,4})\b", remaining)
        if cvc_match:
            cvc = cvc_match.group(1)
            remaining = remaining.replace(cvc_match.group(0), " ", 1)

    # 3. 如果还没提取月份年份
    if not (exp_month and exp_year):
        month_match = re.search(r"\b(0[1-9]|1[0-2])\b", remaining)
        year_match = re.search(r"\b(20[2-3]\d|[2-3]\d)\b", remaining)
        if month_match and year_match:
            exp_month = f"{int(month_match.group(1)):02d}"
            raw_y = year_match.group(1)
            exp_year = f"20{raw_y}" if len(raw_y) == 2 else raw_y

    # 4. 查找可能附带的 5 位美国邮编
    if not hint_zip and remaining:
        zip_match = re.search(r"\b([0-9]{5}(?:-[0-9]{4})?)\b", remaining)
        if zip_match:
            hint_zip = zip_match.group(1)[:5]

    if not (exp_month and exp_year):
        return {"valid": False, "error": "未识别到有效的有效期限 (月/年)"}
    if not cvc or len(cvc) not in (3, 4):
        return {"valid": False, "error": "未识别到有效的 3~4 位安全码 (CVV/CVC)"}

    return {
        "valid": True,
        "card_number": card_number,
        "last4": last4,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvc": cvc,
        "brand": brand,
        "country": country_code,
        "country_name": country_name,
        "postal_code": hint_zip or None,
        "error": None,
    }


# ==============================================================================
# 2. 美国五大免税州真实账单生成器 (0 消费税保障)
# ==============================================================================

US_TAX_FREE_PROFILES = [
    {"state": "OR", "state_name": "Oregon", "city": "Portland", "postal_code": "97201", "street": "1220 SW Morrison St"},
    {"state": "OR", "state_name": "Oregon", "city": "Portland", "postal_code": "97204", "street": "520 SW Yamhill St"},
    {"state": "OR", "state_name": "Oregon", "city": "Portland", "postal_code": "97209", "street": "1120 NW Couch St"},
    {"state": "OR", "state_name": "Oregon", "city": "Eugene", "postal_code": "97401", "street": "856 Willamette St"},
    {"state": "OR", "state_name": "Oregon", "city": "Salem", "postal_code": "97301", "street": "260 Liberty St SE"},
    {"state": "DE", "state_name": "Delaware", "city": "Wilmington", "postal_code": "19801", "street": "800 N French St"},
    {"state": "DE", "state_name": "Delaware", "city": "Wilmington", "postal_code": "19802", "street": "2400 Washington St"},
    {"state": "DE", "state_name": "Delaware", "city": "Newark", "postal_code": "19711", "street": "100 Main St"},
    {"state": "DE", "state_name": "Delaware", "city": "Dover", "postal_code": "19901", "street": "411 Federal St"},
    {"state": "MT", "state_name": "Montana", "city": "Billings", "postal_code": "59101", "street": "200 N Broadway"},
    {"state": "MT", "state_name": "Montana", "city": "Missoula", "postal_code": "59801", "street": "140 S 4th St W"},
    {"state": "MT", "state_name": "Montana", "city": "Helena", "postal_code": "59601", "street": "300 N Last Chance Gulch"},
    {"state": "NH", "state_name": "New Hampshire", "city": "Manchester", "postal_code": "03101", "street": "1000 Elm St"},
    {"state": "NH", "state_name": "New Hampshire", "city": "Nashua", "postal_code": "03060", "street": "221 Main St"},
]

COMMON_US_FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles"]
COMMON_US_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]


def generate_tax_free_billing(country: str = "US", hint_zip: str | None = None) -> dict[str, str]:
    """生成合规且税费为 $0.00 的真实账单地址。"""
    first_name = random.choice(COMMON_US_FIRST_NAMES)
    last_name = random.choice(COMMON_US_LAST_NAMES)
    full_name = f"{first_name} {last_name}"

    chosen = None
    if hint_zip:
        hint_zip = str(hint_zip).strip()[:5]
        for p in US_TAX_FREE_PROFILES:
            if p["postal_code"] == hint_zip:
                chosen = p
                break

    if not chosen:
        chosen = random.choice(US_TAX_FREE_PROFILES)

    building_num = random.randint(100, 9999)
    street_name = chosen["street"].split(" ", 1)[-1] if " " in chosen["street"] else chosen["street"]
    line1 = f"{building_num} {street_name}"

    return {
        "name": full_name,
        "country": country.upper(),
        "line1": line1,
        "line2": "",
        "city": chosen["city"],
        "state": chosen["state"],
        "postal_code": chosen["postal_code"],
        "state_name": chosen["state_name"],
    }


# ==============================================================================
# 3. 原生 Stripe Checkout 直链生成器 (免 CDK 自研方案)
# ==============================================================================

def extract_native_checkout_url(
    access_token: str,
    proxy_url: str = "",
    with_promo: bool = True,
    country: str = "US",
) -> dict[str, Any]:
    """
    使用账号 access_token 原生向 OpenAI 请求 Stripe Checkout 页面链接。
    不依赖任何外部商业提链接口与 CDK。
    """
    token = (access_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("缺少 access_token，无法创建结账会话")

    from core.session import BrowserSession
    session = BrowserSession(proxy=proxy_url or None, detect_exit_geo=False)

    country = country.upper()
    currency = "USD" if country == "US" else "EUR"

    json_body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "checkout_ui_mode": "custom",
        "billing_details": {
            "country": country,
            "currency": currency,
        },
    }
    if with_promo:
        json_body["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers.update({
        "authorization": f"Bearer {token}",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    })

    resp = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=json_body,
        headers=headers,
        timeout=35,
    )

    if resp.status_code >= 400:
        err_msg = resp.text[:300]
        if "user is already paid" in err_msg.lower():
            return {
                "ok": False,
                "already_paid": True,
                "error": "该账号已是 Plus 会员 (User is already paid)",
                "url": None,
            }
        raise RuntimeError(f"OpenAI 结账会话创建失败 HTTP {resp.status_code}: {err_msg}")

    data = resp.json() or {}
    url = data.get("url")
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")

    if not url and cs_id:
        url = f"https://checkout.stripe.com/c/pay/{cs_id}"

    if not url:
        raise RuntimeError(f"OpenAI 响应未包含有效结账链接: {str(data)[:300]}")

    return {
        "ok": True,
        "already_paid": False,
        "url": url,
        "checkout_session_id": cs_id,
        "processor_entity": data.get("processor_entity") or "openai_llc",
        "stripe_publishable_key": data.get("stripe_publishable_key"),
        "error": None,
    }


# ==============================================================================
# 4. 基于 CloakBrowser 与美区住宅代理的全自动绑卡引擎
# ==============================================================================

def _pick_best_proxy_for_card(country: str = "US") -> str:
    """按卡片归属国从 proxy_pool 抽取活跃住宅代理，兜底回退通用有效代理。"""
    try:
        from core.db import pick_proxy_by_country
        p = pick_proxy_by_country(country)
        if p:
            return p
    except Exception:
        pass
    from config.proxy import pick_proxy
    return pick_proxy() or ""


def bind_card_with_cloak(
    account_id: int,
    card_info: dict[str, Any],
    proxy_url: str | None = None,
    log_cb: Any = None,
) -> dict[str, Any]:
    """
    使用 CloakBrowser 指纹浏览器全自动完成绑卡。
    """
    def _emit(msg: str):
        logger.info("[自动绑卡 #%s] %s", account_id, msg)
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    acc = db.get_account(account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}

    token = (acc.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "error": "账号缺少 access_token"}

    # 1. 自动选择匹配卡片发行国的美区住宅代理
    country = card_info.get("country", "US")
    selected_proxy = proxy_url or _pick_best_proxy_for_card(country)
    _emit(f"卡片识别: {card_info.get('brand')} *{card_info.get('last4')} 归属国: {country}")
    _emit(f"代理路由: 分配住宅代理 {selected_proxy.split('@')[-1] if '@' in selected_proxy else (selected_proxy or '直连')}")

    # 2. 生成账单地址
    billing = generate_tax_free_billing(country=country, hint_zip=card_info.get("postal_code"))
    _emit(f"账单地址: 免税州 {billing['state_name']} ({billing['city']}, {billing['postal_code']}) 消费税: $0.00")

    # 3. 获取原生 Stripe Checkout URL
    _emit("正在向 OpenAI 申请 Checkout 支付会话…")
    try:
        checkout_data = extract_native_checkout_url(
            access_token=token,
            proxy_url=selected_proxy,
            with_promo=True,
            country=country,
        )
    except Exception as exc:
        err = f"获取支付会话失败: {exc}"
        _emit(err)
        return {"ok": False, "error": err}

    if checkout_data.get("already_paid"):
        _emit("检测到账号已经是 Plus 会员，自动校准状态")
        db.update_account_card_binding(account_id, {
            "ok": True,
            "status": "success",
            "message": "账号已是 Plus 会员",
            "card_brand": card_info.get("brand"),
            "card_last4": card_info.get("last4"),
        })
        return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}

    checkout_url = checkout_data.get("url")
    _emit(f"支付直链已就绪: {checkout_url[:60]}…")

    # 4. 启动 CloakBrowser 挂载住宅代理
    _emit("启动 CloakBrowser 原生抗封指纹浏览器…")
    from core.cloakbrowser_driver import build_cloak_driver

    driver = None
    try:
        driver, _ = build_cloak_driver(proxy=selected_proxy)
        driver.set_page_load_timeout(60)

        _emit("指纹环境就绪，正在加载 Stripe 收银台…")
        driver.get(checkout_url)
        time.sleep(3.5)

        cur_url = driver.current_url or ""
        if "chatgpt.com" in cur_url and ("verify" in cur_url or "success" in cur_url):
            _emit("页面直接完成重定向，开通成功！")
            db.update_account_card_binding(account_id, {
                "ok": True,
                "status": "success",
                "message": "开通 Plus 成功",
                "card_brand": card_info.get("brand"),
                "card_last4": card_info.get("last4"),
            })
            return {"ok": True, "status": "success", "message": "绑卡开通 Plus 成功"}

        _emit("定位收银台输入表单并模拟击键…")
        raw_page = getattr(driver, "page", None)

        if raw_page:
            try:
                # 等待卡号输入框加载（通过多层 frame 探测）
                start_wait = time.time()
                card_input = None
                while time.time() - start_wait < 20:
                    # 尝试在子 iframe 或顶层页面寻找输入框
                    for frame in raw_page.frames:
                        try:
                            loc = frame.locator('input[name="number"], input[name="cardNumber"], #cardNumber')
                            if loc.count() > 0 and loc.first.is_visible():
                                card_input = loc.first
                                break
                        except Exception:
                            pass
                    if card_input:
                        break
                    time.sleep(1.0)

                if card_input:
                    _emit("聚焦卡号输入框，真实击键录入卡号…")
                    card_input.click()
                    card_input.type(card_info["card_number"], delay=random.randint(45, 85))
                    time.sleep(0.5)

                    # 寻找对应 frame 里的有效期与 CVV
                    parent_frame = card_input.frame
                    exp_loc = parent_frame.locator('input[name="expiry"], input[name="cardExpiry"], #cardExpiry')
                    if exp_loc.count() > 0:
                        exp_str = f"{card_info['exp_month']}{card_info['exp_year'][-2:]}"
                        exp_loc.first.click()
                        exp_loc.first.type(exp_str, delay=random.randint(50, 95))
                        time.sleep(0.4)

                    cvc_loc = parent_frame.locator('input[name="cvc"], input[name="cardCvc"], #cardCvc')
                    if cvc_loc.count() > 0:
                        cvc_loc.first.click()
                        cvc_loc.first.type(card_info["cvc"], delay=random.randint(50, 95))
                        time.sleep(0.4)

                # 寻找邮编与人名（通常在顶层页面或账单 frame）
                for frame in raw_page.frames:
                    try:
                        zip_loc = frame.locator('input[name="postalCode"], input[name="billingPostalCode"], #billingPostalCode')
                        if zip_loc.count() > 0 and zip_loc.first.is_visible():
                            zip_loc.first.fill("")
                            zip_loc.first.type(billing["postal_code"], delay=random.randint(40, 80))

                        name_loc = frame.locator('input[name="name"], input[name="billingName"], #billingName')
                        if name_loc.count() > 0 and name_loc.first.is_visible():
                            name_loc.first.fill("")
                            name_loc.first.type(billing["name"], delay=random.randint(30, 70))
                    except Exception:
                        pass

                _emit("表单信息录入完毕，准备提交扣款授权…")
                time.sleep(1.0)

                # 提交按钮
                submit_btn = raw_page.locator(
                    'button[type="submit"], button:has-text("Subscribe"), button:has-text("Start trial"), button:has-text("订阅"), button:has-text("开始试用")'
                ).first
                if submit_btn.count() > 0:
                    submit_btn.click()
                else:
                    driver.execute_script("let b = document.querySelector('button[type=\"submit\"]'); if(b) b.click();")

            except Exception as e:
                _emit(f"表单自动化操作提示: {e}")

        _emit("已提交扣款请求，等待 Stripe 与 OpenAI 结算确认…")
        start_check = time.time()
        success = False
        error_reason = ""

        while time.time() - start_check < 40:
            time.sleep(2.0)
            cur_url = driver.current_url or ""
            if "chatgpt.com" in cur_url and ("verify" in cur_url or "success" in cur_url or "plan_type=plus" in cur_url):
                success = True
                break
            if "pay.openai.com/success" in cur_url or "stripe_session_id" in cur_url:
                success = True
                break

            try:
                err_text = driver.execute_script("""
                let err = document.querySelector('.p-FieldError, .ElementsApp .Error, [role="alert"], div[class*="error"], .SubmitButton-IconContainer--error');
                return err ? (err.innerText || err.textContent) : '';
                """)
                if err_text and str(err_text).strip():
                    error_reason = str(err_text).strip()
                    break
            except Exception:
                pass

        if success:
            _emit("🎉 恭喜！绑卡授权成功，账号已升级为 ChatGPT Plus！")
            db.update_account_card_binding(account_id, {
                "ok": True,
                "status": "success",
                "message": "绑卡开通 Plus 成功",
                "card_brand": card_info.get("brand"),
                "card_last4": card_info.get("last4"),
                "card_bound_at": datetime.now().isoformat(timespec="seconds"),
            })
            return {"ok": True, "status": "success", "message": "开通 Plus 成功"}
        else:
            fail_msg = error_reason or "扣款授权超时或未跳转成功页面"
            _emit(f"❌ 绑卡未成功: {fail_msg}")
            db.update_account_card_binding(account_id, {
                "ok": False,
                "status": "failed",
                "error": fail_msg,
                "message": fail_msg,
                "card_brand": card_info.get("brand"),
                "card_last4": card_info.get("last4"),
            })
            return {"ok": False, "error": fail_msg}

    except Exception as exc:
        err = f"CloakBrowser 执行异常: {exc}"
        _emit(err)
        logger.exception("[自动绑卡] 异常中断")
        db.update_account_card_binding(account_id, {
            "ok": False,
            "status": "failed",
            "error": err,
            "message": err,
        })
        return {"ok": False, "error": err}
    finally:
        if driver:
            try:
                _emit("回收指纹浏览器进程与系统资源…")
                driver.quit()
            except Exception:
                pass


def enqueue_card_binding(
    account_id: int,
    raw_card_input: str,
    mode: str = "auto",
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """提交绑卡任务或生成直链。"""
    parsed = parse_card_input(raw_card_input)
    if not parsed.get("valid"):
        return {"accepted": False, "error": parsed.get("error")}

    acc = db.get_account(account_id)
    if not acc:
        return {"accepted": False, "error": "账号不存在"}

    if mode == "link_only":
        token = (acc.get("access_token") or "").strip()
        country = parsed.get("country", "US")
        p = proxy_url or _pick_best_proxy_for_card(country)
        try:
            res = extract_native_checkout_url(access_token=token, proxy_url=p, with_promo=True, country=country)
            if res.get("url"):
                db.update_account_extract(account_id, {
                    "ok": True,
                    "status": "success",
                    "link_type": "card",
                    "result": {
                        "long_url": res["url"],
                        "payment_method": "card",
                        "expires_at": int(time.time()) + 86400,
                    }
                })
            return {
                "accepted": True,
                "mode": "link_only",
                "url": res.get("url"),
                "already_paid": res.get("already_paid"),
                "card_info": parsed,
            }
        except Exception as exc:
            return {"accepted": False, "error": str(exc)}

    # 自动绑卡模式
    db.mark_account_card_binding_running(account_id)
    job_id = f"bind_{account_id}_{int(time.time())}"

    with _BINDING_JOBS_LOCK:
        _BINDING_JOBS[job_id] = {
            "job_id": job_id,
            "account_id": account_id,
            "status": "running",
            "logs": ["任务已加入后台队列"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _task():
        def _log_cb(msg: str):
            with _BINDING_JOBS_LOCK:
                job = _BINDING_JOBS.get(job_id)
                if job:
                    job.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
                    if len(job["logs"]) > 50:
                        job["logs"] = job["logs"][-50:]

        res = bind_card_with_cloak(account_id, parsed, proxy_url=proxy_url, log_cb=_log_cb)
        with _BINDING_JOBS_LOCK:
            job = _BINDING_JOBS.get(job_id)
            if job:
                job["status"] = "success" if res.get("ok") else "failed"
                job["result"] = res

    _BINDING_EXECUTOR.submit(_task)
    return {
        "accepted": True,
        "mode": "auto",
        "job_id": job_id,
        "card_info": parsed,
    }


def get_binding_job_status(job_id: str) -> dict[str, Any] | None:
    with _BINDING_JOBS_LOCK:
        job = _BINDING_JOBS.get(job_id)
        return dict(job) if job else None
