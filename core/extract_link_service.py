# -*- coding: utf-8 -*-
"""
ChatGPT Plus 原生官方试用提链核心服务 (Native Extraction Engine)。
采用纯指纹浏览器环境提链，严格采用账号同属地代理路由，彻底防范风控拦截。
"""
from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from core import db
from core.geo_utils import format_country_badge, get_country_badge_info

logger = logging.getLogger(__name__)

_EXTRACT_WORKERS = 3
_EXTRACT_EXECUTOR = ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS, thread_name_prefix="native-extract")


def extract_checkout_url_with_cloak(
    account: dict,
    proxy_url: str = "",
    log_cb: Any = None,
) -> dict[str, Any]:
    """
    使用 CloakBrowser 指纹浏览器真实环境自动化获取官方 Stripe 试用结账链接。
    """
    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    email = str(account.get("email") or "").strip()
    if not email:
        raise ValueError("账号缺少邮箱信息，无法通过指纹浏览器登录提链")

    totp_secret = str(account.get("totp_secret") or "").strip()
    origin_country = str(account.get("country_code") or "JP").strip().upper() or "JP"

    _emit("启动 CloakBrowser 原生指纹浏览器 (提链环境)…")
    from core.cloakbrowser_driver import build_cloak_driver
    from core.roxy_registration import (
        _find_visible_email_input_js,
        _type_email_address,
        _submit_nearest_form_for_active_input,
        _clear_otp_inputs,
        _type_otp,
        _click_continue,
        _fetch_chatgpt_session,
    )
    from core.email_provider import wait_for_otp

    driver = None
    try:
        driver, _ = build_cloak_driver(proxy=proxy_url)
        driver.set_page_load_timeout(60)

        _emit(f"正在打开 ChatGPT 登录页以建立会话 ({email})…")
        driver.get("https://chatgpt.com/auth/login")
        time.sleep(2.5)

        _emit("进入登录认证流转与安全校验…")
        otp_after_ts = time.time() - 2.0
        email_submitted = False
        otp_submitted = False
        totp_attempts = 0
        max_totp_attempts = 3
        last_logged_url = ""
        last_totp_submit_time = 0.0
        cf_challenge_logged = False

        t_end = time.time() + 120
        while time.time() < t_end:
            cur_url = str(driver.current_url or "")
            title = str(driver.title or "")
            base_url = cur_url.split("?")[0] if cur_url else ""

            if base_url and base_url != last_logged_url:
                last_logged_url = base_url
                _emit(f"页面流转: {base_url}")

            if "error" in cur_url and "rate_limit" in cur_url:
                raise RuntimeError("OpenAI 登录验证码发送过于频繁 (rate_limit_exceeded)，请稍后重试")

            # 1. 成功落地 ChatGPT 首页
            if "chatgpt.com" in cur_url and "auth." not in cur_url and "mfa" not in cur_url and not cur_url.endswith("/auth/login"):
                _emit("已成功登录并进入 ChatGPT！")
                break

            # 2. 检查 Cloudflare 质询
            is_cf_challenge = False
            if any(w in title for w in ["しばらくお待ちください", "Just a moment", "Attention Required"]):
                is_cf_challenge = True
            elif "cloudflare" in cur_url.lower():
                is_cf_challenge = True

            if is_cf_challenge:
                if not cf_challenge_logged:
                    cf_challenge_logged = True
                    _emit("检测到 Cloudflare 人机安全质询，正在自动尝试穿透/等待放行…")
                try:
                    for frame in getattr(driver.page, "frames", []):
                        f_url = str(getattr(frame, "url", "") or "").lower()
                        if "challenges.cloudflare.com" in f_url or "cloudflare" in f_url:
                            box = frame.locator("input[type='checkbox'], .ctp-checkbox-label, #cf-stage").first
                            if box.is_visible():
                                _emit("发现 Cloudflare 人机复选框，正在模拟点击…")
                                box.click()
                                time.sleep(2.0)
                                break
                except Exception:
                    pass
                time.sleep(2.0)
                continue
            else:
                cf_challenge_logged = False

            # 3. 处于邮箱输入页面
            if not email_submitted:
                el = _find_visible_email_input_js(driver)
                if el:
                    _emit("正在提交账号邮箱…")
                    _type_email_address(driver, email, timeout=10)
                    time.sleep(0.8)
                    _submit_nearest_form_for_active_input(driver)
                    email_submitted = True
                    otp_after_ts = time.time() - 2.0
                    time.sleep(2.0)
                    continue
                elif "auth.openai.com" in cur_url:
                    email_submitted = True

            # 4. 处于邮箱验证码 (OTP) 页面
            if not otp_submitted and ("email-verification" in cur_url or "auth.openai.com/u/email-verification" in cur_url):
                _emit("等待接收邮箱验证码 (OTP)…")
                otp_code = wait_for_otp(email, after_ts=otp_after_ts, max_wait=40)
                _emit("收到邮箱验证码，正在模拟输入…")
                _clear_otp_inputs(driver)
                _type_otp(driver, otp_code)
                time.sleep(1.0)
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                otp_submitted = True
                time.sleep(3.0)
                continue

            # 5. 处于 TOTP 2FA 双因子验证挑战页面
            is_mfa_page = False
            if any(k in cur_url.lower() for k in ["mfa", "challenge", "authenticator"]):
                is_mfa_page = True
            else:
                try:
                    body_text = driver.execute_script("return (document.body ? document.body.innerText : '').slice(0, 500);") or ""
                    if any(w in body_text for w in ["認証アプリ", "authenticator", "ワンタイム", "security code", "two-factor", "Two-factor"]):
                        is_mfa_page = True
                except Exception:
                    pass

            if is_mfa_page and (time.time() - last_totp_submit_time >= 5.0) and totp_attempts < max_totp_attempts:
                if not totp_secret:
                    raise RuntimeError("账号触发了双因子 TOTP 验证，但系统内未存储 totp_secret")
                totp_attempts += 1
                last_totp_submit_time = time.time()
                import pyotp
                code = pyotp.TOTP(totp_secret).now()
                _emit(f"检测到双因子 TOTP 挑战 (第 {totp_attempts} 次)，正在计算动态令牌并自动提交…")
                try:
                    from selenium.webdriver.common.by import By
                    inputs = [
                        e for e in driver.find_elements(
                            By.CSS_SELECTOR,
                            "input[name='code'], input[autocomplete='one-time-code'], input[inputmode='numeric'], input[type='text'], input[type='tel']"
                        ) if e.is_displayed()
                    ]
                    if inputs:
                        inp = inputs[0]
                        driver.execute_script("""
                            const el = arguments[0];
                            const val = arguments[1];
                            el.focus();
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(el, val); else el.value = val;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        """, inp, code)
                        try:
                            inp.send_keys(code)
                        except Exception:
                            pass
                        time.sleep(1.0)

                        submit_btns = [
                            b for b in driver.find_elements(
                                By.CSS_SELECTOR,
                                "button[type='submit'], form button, button[data-dd-action-name='Continue']"
                            ) if b.is_displayed()
                        ]
                        if submit_btns:
                            submit_btns[0].click()
                        else:
                            driver.execute_script("""
                                const btn = document.querySelector("button[type='submit'], form button");
                                if (btn) btn.click();
                            """)
                        time.sleep(3.0)
                except Exception as exc:
                    _emit(f"TOTP 提交尝试异常: {exc}")
                    time.sleep(2.0)
                continue

            time.sleep(1.5)

        _emit("正在读取 ChatGPT 真实登录会话凭证…")
        session_info = _fetch_chatgpt_session(driver, timeout=40, auto_jump_wait=30)
        access_token = session_info.get("accessToken") or account.get("access_token")
        account_id = (session_info.get("account") or {}).get("id") or account.get("account_id")

        if not access_token:
            raise RuntimeError("指纹浏览器未能获取到有效 accessToken")

        _emit(f"指纹环境已鉴权，正在向 OpenAI 发起【{origin_country}】原生结账申请…")
        js_checkout = """
        const done = arguments[arguments.length - 1];
        const token = arguments[0];
        const accountId = arguments[1];
        const country = arguments[2] || 'JP';
        const currency = country === 'JP' ? 'JPY' : 'USD';

        const body = {
            entry_point: 'all_plans_pricing_modal',
            plan_name: 'chatgptplusplan',
            checkout_ui_mode: 'hosted',
            billing_details: {
                country: country,
                currency: currency
            },
            promo_campaign: {
                promo_campaign_id: 'plus-1-month-free',
                is_coupon_from_query_param: false
            }
        };

        fetch('https://chatgpt.com/backend-api/payments/checkout', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + token,
                'chatgpt-account-id': accountId,
                'x-openai-target-path': '/backend-api/payments/checkout',
                'x-openai-target-route': '/backend-api/payments/checkout'
            },
            body: JSON.stringify(body)
        })
        .then(async r => {
            let data = {};
            try { data = await r.json(); } catch(e) {}
            done({ status: r.status, ok: r.ok, data: data });
        })
        .catch(err => {
            done({ ok: false, error: String(err) });
        });
        """
        res = driver.execute_async_script(js_checkout, access_token, str(account_id or ""), origin_country)
        if not res or not res.get("ok"):
            # 尝试 custom 模式兜底
            js_custom = js_checkout.replace("'checkout_ui_mode': 'hosted'", "'checkout_ui_mode': 'custom'")
            res = driver.execute_async_script(js_custom, access_token, str(account_id or ""), origin_country)

        checkout_data = res.get("data") if (res and isinstance(res, dict)) else {}
        url = checkout_data.get("url")
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("session_id") or checkout_data.get("id")
        if not url and cs_id:
            url = f"https://checkout.stripe.com/c/pay/{cs_id}"

        if not url:
            err_msg = str(res.get("data") or res.get("error") or "")
            if "already" in err_msg.lower() or "active_subscription" in err_msg.lower():
                return {
                    "ok": True,
                    "already_paid": True,
                    "url": None,
                    "message": "账号已是 Plus 会员",
                }
            raise RuntimeError(f"结账申请返回异常: {str(res)[:200]}")

        return {
            "ok": True,
            "url": url,
            "checkout_session_id": cs_id,
            "error": None,
        }
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _run_extract(*, account_id: int, trigger: str = "manual") -> dict:
    """执行单账号同属地提链任务。"""
    acc = db.get_account(account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}

    email = acc.get("email") or f"ID #{account_id}"
    country_code = str(acc.get("country_code") or "").strip().upper()

    # 若账号缺失属地代码，尝试从历史 proxy_used 反查
    if not country_code and acc.get("proxy_used"):
        p_info = db.find_proxy_by_url(acc["proxy_used"])
        if p_info and p_info.get("country_code"):
            country_code = p_info["country_code"].upper()

    if not country_code:
        # 存量默认兜底按日本试用处理
        country_code = "JP"

    country_badge = format_country_badge(country_code, fallback_country=acc.get("country") or "")

    # 严格匹配同属地活跃代理 (strict=True 绝不跨区回退)
    matching_proxy = db.pick_proxy_by_country(country_code, strict=True)
    if not matching_proxy:
        msg = f"账号属地为【{country_badge}】，但代理池中暂无该属地活跃代理。为防风控拦截，已自动跳过提链。"
        logger.warning("[提链调度] 账号 %s %s", email, msg)
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "skipped",
            "error": msg,
            "message": msg,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        })
        return {"ok": False, "status": "skipped", "message": msg}

    if not db.mark_account_extract_running(account_id):
        return {"ok": False, "error": "任务状态异常或已被占用"}

    logger.info("[提链] 账号 %s 匹配到【%s】活跃代理，启动 CloakBrowser 原生提链…", email, country_badge)
    db.update_account_extract(account_id, {
        "ok": False,
        "status": "running",
        "message": f"正在通过【{country_badge}】代理启动指纹浏览器提链…",
    })

    try:
        res = extract_checkout_url_with_cloak(
            account=acc,
            proxy_url=matching_proxy,
            log_cb=lambda msg: db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "message": msg,
            }),
        )
        if res.get("already_paid"):
            db.update_account_extract(account_id, {
                "ok": True,
                "status": "success",
                "message": "账号已是 Plus 会员",
            })
            logger.info("[提链] 账号 %s 已是 Plus 会员", email)
            return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}

        url = res.get("url")
        if not url:
            raise RuntimeError(f"未提取到有效 Stripe 链接: {res}")

        db.update_account_extract(account_id, {
            "ok": True,
            "status": "success",
            "url": url,
            "link": url,
            "stripe_checkout_url": url,
            "message": "原生提链成功",
        })
        logger.info("[提链] 账号 %s 提链成功: %s", email, url[:60])
        return {"ok": True, "url": url}
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {str(exc)}"
        logger.error("[提链] 账号 %s 提链失败: %s", email, err_msg)
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "failed",
            "error": err_msg,
            "message": f"提链失败: {err_msg[:120]}",
        })
        return {"ok": False, "error": err_msg}


def enqueue_account_extract(
    *,
    account_id: int,
    email: str = "",
    access_token: str = "",
    trigger: str = "manual",
    **kwargs: Any,
) -> dict:
    """将单账号提链任务加入执行队列。"""
    acc = db.get_account(account_id)
    if not acc:
        return {"accepted": False, "busy": False, "error": "账号不存在"}

    if not db.claim_account_extract(account_id, trigger=trigger):
        return {"accepted": False, "busy": True, "error": "该账号提链任务正在执行中"}

    fut = _EXTRACT_EXECUTOR.submit(_run_extract, account_id=account_id, trigger=trigger)
    return {"accepted": True, "busy": False, "future": fut}
