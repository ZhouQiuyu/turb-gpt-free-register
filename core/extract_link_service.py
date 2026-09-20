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
from core.cloudflare_solver import solve_cloudflare_challenge_if_present
from core.geo_utils import format_country_badge, get_country_badge_info

logger = logging.getLogger(__name__)

_EXTRACT_WORKERS = 3
_EXTRACT_EXECUTOR = ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS, thread_name_prefix="native-extract")


def get_currency_for_country(country: str) -> str:
    c = (country or "US").strip().upper()
    if c == "JP":
        return "JPY"
    if c == "KR":
        return "KRW"
    if c == "GB":
        return "GBP"
    if c in {"DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "FI", "IE", "GR"}:
        return "EUR"
    return "USD"


def _human_extract_checkout_url(
    driver: Any,
    promo_campaign_id: str = "",
    emit_fn: Any = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """
    通过真实浏览器拟人化 UI 点击操作触发原生试用提链。
    由 ChatGPT 前端原生运行 Sentinel 人机质询与 PoW 计算，天然绕过风控拦截。
    """
    def _emit(msg: str):
        if emit_fn:
            try:
                emit_fn(msg)
            except Exception:
                pass

    page = getattr(driver, "page", None)
    stripe_url = None
    checkout_response_data = None

    if page:
        def handle_response(response):
            nonlocal stripe_url, checkout_response_data
            url = response.url
            if "/backend-api/payments/checkout" in url:
                try:
                    data = response.json()
                    checkout_response_data = data
                    if isinstance(data, dict):
                        target = data.get("url") or data.get("checkout_session_id")
                        if target:
                            if not target.startswith("http"):
                                target = f"https://checkout.stripe.com/c/pay/{target}"
                            stripe_url = target
                            _emit(f"拦截到官方 Stripe 结账链接: {stripe_url}")
                except Exception:
                    pass
            elif "checkout.stripe.com" in url:
                if not stripe_url:
                    stripe_url = url

        def handle_framenavigated(frame):
            nonlocal stripe_url
            url = frame.url
            if "checkout.stripe.com" in url:
                stripe_url = url

        page.on("response", handle_response)
        page.on("framenavigated", handle_framenavigated)

    # 1. 确保进入 ChatGPT 主界面 (若有活动直接带参唤起定价弹窗)
    cur_url = str(getattr(driver, "current_url", "") or "")
    target_home_url = "https://chatgpt.com/"
    if promo_campaign_id and promo_campaign_id != "none":
        target_home_url = f"https://chatgpt.com/?promo_campaign={promo_campaign_id}#pricing"

    _emit(f"正在导航进入 ChatGPT 主界面 ({target_home_url})…")
    logger.info("[提链-拟人化] 导航进入: %s", target_home_url)
    for attempt in range(3):
        try:
            driver.get(target_home_url)
            break
        except Exception as e:
            if attempt == 2:
                break
            time.sleep(2.0)
    time.sleep(3.5)

    solve_cloudflare_challenge_if_present(driver, max_wait=10.0, emit_fn=_emit)

    # 2. 检查并关闭欢迎/通知弹窗 (Got it / 了解 / 閉じる / dismiss)
    try:
        driver.execute_script("""
            const btns = [...document.querySelectorAll('button')];
            const gotIt = btns.find(b => /got it|了解|閉じる|dismiss|close/i.test(b.innerText || ''));
            if (gotIt && (gotIt.offsetWidth || gotIt.offsetHeight)) {
                gotIt.click();
            }
        """)
        time.sleep(1.5)
    except Exception:
        pass

    # 3. 定位并点击侧边栏 / 菜单「Claim offer / Upgrade / オファー / 特典」按钮
    _emit("正在寻找并点击侧边栏 / 菜单「Claim offer / Upgrade / オファー」入口…")
    upgrade_info = driver.execute_script("""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
        const allButtons = [...document.querySelectorAll('button, a, div[role="button"]')].filter(visible);

        // 1. 优先直接匹配页面上所有包含专属优惠/试用关键词的按钮 (中英日越)
        const targetBtn = allButtons.find(b => {
            const t = (b.innerText || '').trim().toLowerCase();
            return (
                t.includes('nhận ưu đãi') ||
                t.includes('claim offer') ||
                t.includes('オファーを受け取る') ||
                t.includes('特典を受け取る') ||
                t.includes('claim') ||
                t.includes('offer') ||
                t.includes('ưu đãi') ||
                t.includes('特典') ||
                t.includes('オファー') ||
                t.includes('upgrade to plus') ||
                t.includes('plus にアップグレード') ||
                t.includes('nâng cấp lên plus') ||
                t.includes('upgrade') ||
                t.includes('アップグレード') ||
                t.includes('nâng cấp')
            ) && !t.includes('login') && !t.includes('signin') && !t.includes('lên go') && !t.includes('lên pro');
        });
        if (targetBtn) {
            targetBtn.scrollIntoView({ block: 'center' });
            const r = targetBtn.getBoundingClientRect();
            return { ok: true, text: targetBtn.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }

        // 2. 备用常见选择器
        const selectors = [
            'button[aria-label*="Claim offer"]',
            'button[aria-label*="オファー"]',
            'button[aria-label*="特典"]',
            'button[aria-label*="Nhận ưu đãi"]',
            'button[aria-label*="ưu đãi"]',
            'button[aria-label*="Nâng cấp"]',
            'button[aria-label*="アップグレード"]',
            'button[data-testid="upgrade-button"]',
            'button[data-testid="pricing-button"]',
            'button[data-testid="sidebar-upgrade-button"]',
            'a[href*="/pricing"]'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && visible(el)) {
                el.scrollIntoView({ block: 'center' });
                const r = el.getBoundingClientRect();
                return { ok: true, selector: sel, text: el.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }
        }
        return { ok: false };
    """)
    logger.info("[提链-拟人化] 定位升级/优惠入口: %s", upgrade_info)
    if upgrade_info and upgrade_info.get("ok") and upgrade_info.get("x") and upgrade_info.get("y"):
        x = float(upgrade_info["x"])
        y = float(upgrade_info["y"])
        if page and hasattr(page, "mouse") and x > 0 and y > 0:
            page.mouse.move(x, y)
            time.sleep(0.08)
            page.mouse.down()
            time.sleep(0.06)
            page.mouse.up()

    time.sleep(2.5)
    if stripe_url:
        return {"ok": True, "url": stripe_url, "checkout_session_id": stripe_url.split("/")[-1]}

    # 4. 检查是否弹出定价 / 优惠弹窗
    t_wait_modal = time.time() + 6.0
    modal_opened = False
    while time.time() < t_wait_modal and not stripe_url:
        has_dialog = driver.execute_script("""
            const d = document.querySelector('div[role="dialog"], [data-testid="pricing-modal"], [data-testid="all-plans-modal"], div[aria-modal="true"]');
            return !!(d && (d.offsetWidth || d.offsetHeight));
        """)
        if has_dialog:
            modal_opened = True
            break
        time.sleep(1.0)

    if not modal_opened and not stripe_url:
        # 尝试在个人菜单中点击 Upgrade / Claim 项
        menu_info = driver.execute_script("""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const menuItems = [...document.querySelectorAll('[role="menuitem"], button, div')].filter(visible);
            const upgradeItem = menuItems.find(el => {
                const t = (el.innerText || '').toLowerCase();
                return (
                    t.includes('claim offer') ||
                    t.includes('claim') ||
                    t.includes('nhận ưu đãi') ||
                    t.includes('ưu đãi') ||
                    t.includes('nâng cấp') ||
                    t.includes('upgrade') ||
                    t.includes('アップグレード') ||
                    t.includes('plus') ||
                    t.includes('特典') ||
                    t.includes('オファー')
                ) && !t.includes('login');
            });
            if (upgradeItem) {
                upgradeItem.scrollIntoView({ block: 'center' });
                const r = upgradeItem.getBoundingClientRect();
                return { ok: true, text: upgradeItem.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }
            return { ok: false };
        """)
        logger.info("[提链-拟人化] 个人菜单定位结果: %s", menu_info)
        if menu_info and menu_info.get("ok") and menu_info.get("x") and menu_info.get("y"):
            if page and hasattr(page, "mouse"):
                page.mouse.move(float(menu_info["x"]), float(menu_info["y"]))
                time.sleep(0.08)
                page.mouse.down()
                time.sleep(0.06)
                page.mouse.up()
        time.sleep(2.5)

    # 保存弹窗截图供排查
    try:
        driver.save_screenshot("/tmp/extract_modal.png")
    except Exception:
        pass

    # 5. 在定价 / 优惠弹窗中点击确认按钮 (执行真实鼠标坐标点击，触发 React 与 isTrusted 事件)
    if not stripe_url:
        _emit("正在定价/优惠弹窗中点击 Plus 试用确认按钮…")
        btn_info = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const dialog = document.querySelector('div[role="dialog"], div[aria-modal="true"]') || document.body;
            const buttons = [...dialog.querySelectorAll('button')].filter(visible);

            // 优先匹配包含明确优惠 / 试用动作的按钮（中英日越全覆盖，且坚决排除 Go / Pro / Team）
            const plusBtn = buttons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (/(?:^|\s)(?:go|pro|team|business|enterprise)(?:\s|$)/.test(t) && !t.includes('plus')) {
                    return false;
                }
                if (t.includes('lên go') || t.includes('lên pro')) {
                    return false;
                }
                return /dùng thử ưu đãi đặc biệt|ưu đãi đặc biệt|dùng thử plus|nâng cấp lên plus|claim special offer|special offer|try special offer|claim offer|upgrade to plus|plus を試す|無料で試す|plus にアップグレード|特別オファー|オファーを受け取る|特典を受け取る|オファーを利用|特典を利用|オファー|特典|try for free|try plus|get plus|get offer|claim/i.test(t);
            }) || buttons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('lên go') || t.includes('lên pro')) return false;
                return /plus|ưu đãi|オファー|特典|offer/i.test(t);
            }) || dialog.querySelector('button.btn-primary, button[data-testid*="upgrade"], button[data-testid*="claim"]');

            if (plusBtn) {
                plusBtn.scrollIntoView({ block: 'center' });
                const r = plusBtn.getBoundingClientRect();
                return {
                    ok: true,
                    text: plusBtn.innerText.trim(),
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2
                };
            }

            return { ok: false, all_buttons: buttons.map(b => b.innerText.trim()).filter(Boolean) };
        """)
        logger.info("[提链-拟人化] 弹窗内确认按钮定位: %s", btn_info)
        if btn_info and btn_info.get("ok") and btn_info.get("x") and btn_info.get("y"):
            x = float(btn_info["x"])
            y = float(btn_info["y"])
            if page and hasattr(page, "mouse") and x > 0 and y > 0:
                logger.info("[提链-拟人化] 执行真实鼠标轨迹点击按钮 '%s' at (%s, %s)", btn_info.get("text"), x, y)
                page.mouse.move(x, y)
                time.sleep(0.1)
                page.mouse.down()
                time.sleep(0.08)
                page.mouse.up()
            else:
                driver.execute_script("""
                    const dialog = document.querySelector('div[role="dialog"], div[aria-modal="true"]') || document.body;
                    const btn = dialog.querySelector('button');
                    if (btn) btn.click();
                """)

    # 6. 等待捕获 Stripe Checkout 链接 (Sentinel PoW 计算需 40~90s)
    _emit("等待官方生成 Stripe 结账链接 (含 Sentinel 人机对抗计算，最长等待 120 秒)…")
    logger.info("[提链-拟人化] 开始等待 Stripe 链接生成 (最长 120s)…")
    wait_start = time.time()
    while time.time() - wait_start < timeout:
        if stripe_url:
            break
        cur = str(getattr(driver, "current_url", "") or "")
        if "checkout.stripe.com" in cur:
            stripe_url = cur
            break
        time.sleep(1.0)

    if not stripe_url:
        time.sleep(3.0)

    if stripe_url:
        cs_id = stripe_url.split("/")[-1]
        _emit("🎉 官方 Stripe 试用结账链接提取成功！")
        logger.info("[提链-拟人化] 🎉 成功捕获 Stripe 链接: %s", stripe_url)
        return {"ok": True, "url": stripe_url, "checkout_session_id": cs_id}

    if checkout_response_data and isinstance(checkout_response_data, dict):
        err_msg = str(checkout_response_data.get("error") or checkout_response_data.get("detail") or "")
        if "already" in err_msg.lower() or "active_subscription" in err_msg.lower():
            return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}

    logger.warning("[提链-拟人化] 未能通过拟人化操作捕获到链接，当前 URL: %s, 响应: %s", getattr(driver, "current_url", ""), checkout_response_data)
    return {"ok": False, "error": "未能通过拟人化操作捕获到 Stripe 结账链接"}


def _execute_js_checkout(
    driver: Any,
    access_token: str,
    account_id: str,
    country: str,
    currency: str,
    promo_campaign_id: str = "",
) -> dict[str, Any]:
    """在当前已建立好边缘/盾环境的浏览器中，通过真实前端上下文发起原生 checkout 请求。"""
    js_checkout = """
    const done = arguments[arguments.length - 1];
    const token = arguments[0];
    const accountId = arguments[1];
    const country = arguments[2] || 'JP';
    const currency = arguments[3] || 'USD';
    const promoCampaignId = arguments[4] || '';

    const body = {
        entry_point: 'all_plans_pricing_modal',
        plan_name: 'chatgptplusplan',
        checkout_ui_mode: 'hosted',
        billing_details: {
            country: country,
            currency: currency
        }
    };

    if (promoCampaignId && promoCampaignId !== 'none') {
        body.promo_campaign = {
            promo_campaign_id: promoCampaignId,
            is_coupon_from_query_param: false
        };
    }

    const headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token
    };
    if (accountId) {
        headers['chatgpt-account-id'] = accountId;
    }
    try {
        const deviceId = localStorage.getItem('oai-device-id') || (document.cookie.match(/oai-device-id=([^;]+)/) || [])[1];
        if (deviceId) {
            headers['oai-device-id'] = deviceId;
        }
    } catch (_) {}

    fetch('https://chatgpt.com/backend-api/payments/checkout', {
        method: 'POST',
        credentials: 'include',
        headers: headers,
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
    res = driver.execute_async_script(js_checkout, access_token, str(account_id or ""), country, currency, promo_campaign_id)
    if not res or (not res.get("ok") and res.get("status") not in (400, 401)):
        # 仅在非明确业务拦截时尝试 custom 模式兜底
        js_custom = js_checkout.replace("'checkout_ui_mode': 'hosted'", "'checkout_ui_mode': 'custom'")
        res = driver.execute_async_script(js_custom, access_token, str(account_id or ""), country, currency, promo_campaign_id)

    if not res or not isinstance(res, dict):
        return {"ok": False, "error": "JS checkout returned invalid response"}

    checkout_data = res.get("data") if isinstance(res.get("data"), dict) else {}
    url = checkout_data.get("url")
    cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("session_id") or checkout_data.get("id")
    if not url and cs_id:
        url = f"https://checkout.stripe.com/c/pay/{cs_id}"

    if url:
        return {
            "ok": True,
            "url": url,
            "checkout_session_id": cs_id,
            "error": None,
        }

    err_msg = str(res.get("data") or res.get("error") or "")
    if "already" in err_msg.lower() or "active_subscription" in err_msg.lower():
        return {
            "ok": True,
            "already_paid": True,
            "url": None,
            "message": "账号已是 Plus 会员",
        }

    if res.get("status") == 401:
        return {
            "ok": False,
            "status": 401,
            "unauthorized": True,
            "error": "401 Unauthorized",
        }

    return {
        "ok": False,
        "status": res.get("status"),
        "error": err_msg or f"HTTP {res.get('status')}",
        "data": checkout_data,
    }


def is_jwt_expired(token: str) -> bool:
    """本地纯标准库解析 JWT payload 校验 exp，避免引入第三方依赖。"""
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        import base64
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        exp = payload.get("exp")
        if exp and isinstance(exp, (int, float)):
            return time.time() >= float(exp)
    except Exception:
        pass
    return False


def extract_checkout_url_with_cloak(
    account: dict,
    proxy_url: str = "",
    log_cb: Any = None,
) -> dict[str, Any]:
    """
    使用 CloakBrowser 指纹浏览器真实环境自动化获取官方 Stripe 试用结账链接。
    优先使用存量有效 Token 进行会话直通获取；若 Token 缺失或失效，无缝走浏览器登录自愈流。
    """
    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    email = str(account.get("email") or "").strip()
    if not email:
        raise ValueError("账号缺少邮箱信息，无法通过指纹浏览器提链")

    account_id_db = account.get("id")
    access_token = str(account.get("access_token") or "").strip()
    account_id = str(account.get("account_id") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").strip()
    origin_country = str(account.get("country_code") or "JP").strip().upper() or "JP"
    currency = get_currency_for_country(origin_country)
    promo_campaign_id = str(
        account.get("plus_trial_campaign_id")
        or account.get("promo_campaign_id")
        or ""
    ).strip()
    password = str(account.get("password") or account.get("account_password") or account.get("openai_password") or "").strip()

    # 本地校验 JWT 是否已过期
    token_expired = bool(account.get("token_expired") or is_jwt_expired(access_token))

    _emit("启动 CloakBrowser 原生指纹浏览器 (提链环境)…")
    from core.cloakbrowser_driver import build_cloak_driver

    driver = None
    try:
        driver, _ = build_cloak_driver(proxy=proxy_url)
        driver.set_page_load_timeout(60)

        # -------------------------------------------------------------
        # 阶段一：会话直通（若存在存量未过期 access_token）
        # -------------------------------------------------------------
        if access_token and not token_expired:
            _emit("检测到存量会话凭证，正在打开 ChatGPT 建立指纹与边缘环境…")
            driver.get("https://chatgpt.com/")
            time.sleep(2.0)
            solve_cloudflare_challenge_if_present(driver, max_wait=15.0, emit_fn=_emit)

            _emit(f"正在通过真实浏览器环境发起【{origin_country}】原生结账申请…")
            chk_res = _execute_js_checkout(driver, access_token, account_id, origin_country, currency, promo_campaign_id=promo_campaign_id)

            # 1. 成功出链
            if chk_res.get("ok") and chk_res.get("url"):
                _emit("原生结账链接提取成功！")
                return chk_res

            # 2. 账号已是 Plus
            if chk_res.get("already_paid"):
                _emit("账号已是 Plus 会员")
                return chk_res

            # 3. 若返回 401，说明 token 实际已过期/被注销，准备进入阶段二登录流
            if chk_res.get("unauthorized") or chk_res.get("status") == 401:
                _emit("存量会话凭据已失效 (401)，正在切换至浏览器登录自愈流…")
                access_token = ""
            else:
                err_text = str(chk_res.get("error") or "")
                if "already" in err_text.lower():
                    return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}
                _emit(f"会话直通申请未成功 ({err_text[:80]})，切换至完整登录重试…")
                access_token = ""

        # -------------------------------------------------------------
        # 阶段二：浏览器完整登录自愈流 (自愈登录状态机)
        # -------------------------------------------------------------
        if not access_token:
            from core.roxy_registration import (
                _find_visible_email_input_js,
                _type_email_address,
                _submit_nearest_form_for_active_input,
                _clear_otp_inputs,
                _type_otp,
                _click_continue,
                _click_passwordless_signup_if_present,
                _fetch_chatgpt_session,
                _read_chatgpt_session_once,
            )
            from core.email_provider import wait_for_otp

            _emit(f"正在打开 ChatGPT 登录页以建立新会话 ({email})…")
            driver.get("https://chatgpt.com/auth/login")
            time.sleep(2.5)

            otp_after_ts = time.time() - 2.0
            email_submitted = False
            otp_submitted = False
            last_otp_submit_ts = 0.0
            totp_attempts = 0
            max_totp_attempts = 3
            last_logged_url = ""
            last_totp_submit_time = 0.0

            t_end = time.time() + 150
            while time.time() < t_end:
                cur_url = str(driver.current_url or "")
                title = str(driver.title or "")
                base_url = cur_url.split("?")[0] if cur_url else ""

                if base_url and base_url != last_logged_url:
                    last_logged_url = base_url
                    _emit(f"页面流转: {base_url}")

                if "error" in cur_url and "rate_limit" in cur_url:
                    raise RuntimeError("OpenAI 登录验证码发送过于频繁 (rate_limit_exceeded)，请稍后重试")

                # 1. 穿透 Cloudflare 质询
                if solve_cloudflare_challenge_if_present(driver, max_wait=15.0, emit_fn=_emit):
                    time.sleep(1.0)
                    continue

                # 1.5. 检测并恢复 OpenAI 认证页 500 / Route Error / 不明なエラーが発生しました
                is_route_error = driver.execute_script("""
                    const t = (document.body ? document.body.innerText : '').toLowerCase();
                    if (t.includes('route error') || t.includes('500 internal server') || t.includes('不明なエラーが発生しました')) {
                        const btn = [...document.querySelectorAll('button')].find(b => /try again|もう一度試す/i.test(b.innerText || ''));
                        if (btn && (btn.offsetWidth || btn.offsetHeight)) {
                            btn.click();
                            return 'clicked_try_again';
                        }
                        return 'need_refresh';
                    }
                    return null;
                """)
                if is_route_error:
                    _emit(f"检测到 OpenAI 认证页临时异常 ({is_route_error})，正在自动重试恢复…")
                    time.sleep(2.0)
                    if is_route_error == "need_refresh":
                        driver.refresh()
                    time.sleep(3.0)
                    continue

                # 2. 真实登录态判定 (严禁仅凭 cur_url 包含 chatgpt.com 判断！)
                session_data = None
                try:
                    session_data = _read_chatgpt_session_once(driver)
                except Exception:
                    pass

                if session_data and session_data.get("accessToken"):
                    access_token = session_data.get("accessToken")
                    account_id = (session_data.get("account") or {}).get("id") or account_id
                    _emit("浏览器已成功获取到全新登录会话！")
                    if account_id_db:
                        try:
                            from core import db
                            db.update_account_session(
                                acc_id=account_id_db,
                                access_token=access_token,
                                account_id=account_id,
                            )
                        except Exception as exc:
                            logger.warning("[提链] 更新账号会话失败: %s", exc)
                    break

                # 3. 若落在游客聊天首页（例如 ?slm=1 且未有邮箱输入框），点击登录按钮唤起登录弹窗
                if not email_submitted and ("slm=1" in cur_url or cur_url.rstrip("/") in ("https://chatgpt.com", "http://chatgpt.com")):
                    try:
                        clicked = driver.execute_script("""
                            const btn = document.querySelector('button[data-testid="login-button"], [data-testid="login-button"], a[href*="/auth/login"]');
                            if (btn && (btn.offsetWidth || btn.offsetHeight)) { btn.click(); return true; }
                            return false;
                        """)
                        if clicked:
                            _emit("处于匿名首页，已点击登录按钮拉起登录框…")
                            time.sleep(2.0)
                            continue
                    except Exception:
                        pass

                # 4. 处于邮箱输入页面
                if not email_submitted:
                    el = _find_visible_email_input_js(driver)
                    if el:
                        _emit("正在提交账号邮箱…")
                        _type_email_address(driver, email, timeout=10)
                        time.sleep(0.8)
                        submitted = _submit_nearest_form_for_active_input(driver)
                        if not submitted:
                            try:
                                clicked = driver.execute_script("""
                                    const btn = document.querySelector('form button[type="submit"], button.btn-primary');
                                    if (btn && (btn.offsetWidth || btn.offsetHeight)) {
                                        btn.click();
                                        return true;
                                    }
                                    return false;
                                """)
                                if clicked:
                                    submitted = True
                            except Exception:
                                pass
                        if submitted:
                            email_submitted = True
                            otp_after_ts = time.time() - 2.0
                            time.sleep(2.0)
                            continue
                    elif "auth.openai.com" in cur_url and not any(k in cur_url for k in ["login/password", "email-verification", "mfa", "challenge"]):
                        email_submitted = True

                # 5. 处于密码输入页面
                if email_submitted:
                    has_password_input = driver.execute_script("""
                        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
                          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                        const pwd = document.querySelector('input[type="password"], input[name="password"], input[autocomplete="current-password"]');
                        return !!(pwd && visible(pwd));
                    """)
                    if has_password_input:
                        if password:
                            _emit("检测到密码输入框，正在输入密码…")
                            driver.execute_script("""
                                const el = document.querySelector('input[type="password"], input[name="password"], input[autocomplete="current-password"]');
                                const val = arguments[0];
                                el.focus();
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(el, val); else el.value = val;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            """, password)
                            time.sleep(0.5)
                            _submit_nearest_form_for_active_input(driver)
                            time.sleep(2.5)
                            continue
                        else:
                            _emit("检测到密码输入框但账号无密码，正在切换至一次性验证码登录…")
                            res = _click_passwordless_signup_if_present(driver)
                            time.sleep(2.0)
                            continue

                # 6. 处于 TOTP 2FA 双因子验证挑战页面 (必须优先于 OTP 检测，因 MFA 挑战页面同样包含 one-time-code 输入框)
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

                # 7. 处于邮箱验证码 (OTP) 页面 (严格排除 MFA 页面及已跳转至主站页面的情况)
                is_otp_page = ("email-verification" in cur_url or "auth.openai.com/u/email-verification" in cur_url)
                if not is_otp_page and not is_mfa_page and "chatgpt.com" not in cur_url:
                    is_otp_page = bool(driver.execute_script("""
                        return !!document.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"]');
                    """))

                if is_otp_page and (time.time() - last_otp_submit_ts > 30.0):
                    _emit("等待接收邮箱验证码 (OTP)…")
                    try:
                        otp_code = wait_for_otp(email, after_ts=otp_after_ts, max_wait=40, force_service=True)
                    except Exception as exc:
                        exc_str = str(exc)
                        if "D0004" in exc_str:
                            raise RuntimeError(f"该账号关联的临时邮箱已过服务商保留期 (MailNest D0004)，无法接收验证码: {email}") from exc
                        raise

                    # 检查等待期间页面是否已经自动跳转完成登录
                    cur_now = str(getattr(driver, "current_url", "") or "")
                    if "chatgpt.com" in cur_now and "login" not in cur_now and "auth.openai.com" not in cur_now:
                        _emit("页面已在等待期间自动完成登录跳转，跳过验证码输入…")
                        continue

                    _emit("收到邮箱验证码，正在模拟输入…")
                    _clear_otp_inputs(driver)
                    _type_otp(driver, otp_code)
                    time.sleep(1.0)
                    try:
                        _click_continue(driver)
                    except Exception:
                        pass
                    last_otp_submit_ts = time.time()
                    otp_submitted = True
                    time.sleep(3.0)
                    continue

                time.sleep(1.5)

            if not access_token:
                _emit("正在读取 ChatGPT 真实登录会话凭证…")
                session_info = _fetch_chatgpt_session(driver, timeout=30, auto_jump_wait=15)
                access_token = session_info.get("accessToken")
                account_id = (session_info.get("account") or {}).get("id") or account_id

            if not access_token:
                raise RuntimeError("指纹浏览器未能获取到有效 accessToken，登录未完成")

            _emit(f"指纹环境已鉴权，正在通过拟人化操作向 OpenAI 发起【{origin_country}】原生试用提链…")
            chk_res = _human_extract_checkout_url(driver, promo_campaign_id=promo_campaign_id, emit_fn=_emit, timeout=120.0)
            if chk_res.get("ok") and chk_res.get("url"):
                return chk_res
            if chk_res.get("already_paid"):
                return chk_res

            _emit("拟人化未直接出链，尝试通过页面上下文协议兜底…")
            chk_res = _execute_js_checkout(driver, access_token, account_id, origin_country, currency, promo_campaign_id=promo_campaign_id)
            if chk_res.get("ok") and chk_res.get("url"):
                return chk_res
            if chk_res.get("already_paid"):
                return chk_res
            err_msg = str(chk_res.get("data") or chk_res.get("error") or "")
            if "already" in err_msg.lower() or "active_subscription" in err_msg.lower():
                return {"ok": True, "already_paid": True, "url": None, "message": "账号已是 Plus 会员"}
            raise RuntimeError(f"结账申请返回异常: {str(chk_res)[:200]}")
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
    original_country_code = str(acc.get("country_code") or "").strip().upper()
    country_code = original_country_code or "JP"

    # 优先匹配同属地活跃代理；对于未标记国别的存量账号，优先 JP，若无活跃 JP 代理则平滑降级至任意活跃代理
    matching_proxy = db.pick_proxy_by_country(country_code, strict=bool(original_country_code))
    if not matching_proxy and not original_country_code:
        matching_proxy = db.pick_proxy_by_country(country_code, strict=False)

    if matching_proxy:
        p_info = db.find_proxy_by_url(matching_proxy)
        if p_info and p_info.get("country_code"):
            country_code = str(p_info.get("country_code")).strip().upper()

    country_badge = format_country_badge(country_code, fallback_country=acc.get("country") or "")

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

    acc_for_extract = dict(acc)
    acc_for_extract["country_code"] = country_code

    try:
        res = extract_checkout_url_with_cloak(
            account=acc_for_extract,
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
