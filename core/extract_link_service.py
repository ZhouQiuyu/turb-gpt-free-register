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
from pathlib import Path
from typing import Any

from core import db
from core.cloudflare_solver import solve_cloudflare_challenge_if_present
from core.email_provider import wait_for_otp
from core.geo_utils import format_country_badge, get_country_badge_info
from core.roxy_registration import (
    _clear_otp_inputs,
    _click_continue,
    _click_passwordless_signup_if_present,
    _fetch_chatgpt_session,
    _find_visible_email_input_js,
    _read_chatgpt_session_once,
    _submit_email_and_wait_next,
    _submit_email_step,
    _submit_nearest_form_for_active_input,
    _type_email_address,
    _type_otp,
)

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(account_id: int) -> Path:
    return _LOG_DIR / f"extract-link-{int(account_id)}.log"


def _append_log(account_id: int, message: str, *, clear: bool = False) -> None:
    try:
        path = log_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if clear else "a"
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(f"{datetime.now().strftime('%H:%M:%S')} [INFO] {message}\n")
    except Exception:
        pass


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


def _find_visible_password_input_js(driver: Any) -> bool:
    try:
        return bool(driver.execute_script("""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
            const pwd = document.querySelector('input[type="password"], input[name*="password" i], input[autocomplete="current-password"]');
            return !!(pwd && visible(pwd));
        """))
    except Exception:
        return False


def _human_extract_checkout_url(
    driver: Any,
    promo_campaign_id: str = "",
    emit_fn: Any = None,
    timeout: float = 120.0,
    origin_country: str = "JP",
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
    checkout_session_id = None
    captured_pk = None

    if page:
        # 0. 注册 CDP 路由拦截器：将 checkout_ui_mode 重写为 hosted 以直接获取 Stripe 免登托管长链
        if hasattr(page, "route"):
            def handle_route(route, request):
                try:
                    if "/backend-api/payments/checkout" in request.url and request.method == "POST":
                        post_data = request.post_data
                        if post_data:
                            try:
                                payload = json.loads(post_data)
                                logger.info("[CDP] 拦截到 /payments/checkout 请求，原 mode=%s", payload.get("checkout_ui_mode"))
                                payload["checkout_ui_mode"] = "hosted"
                                route.continue_(post_data=json.dumps(payload))
                                logger.info("[CDP] 已将 checkout_ui_mode 重写为 hosted 并放行")
                                return
                            except Exception as ex:
                                logger.warning("[CDP] 解析/重写 post_data 失败: %s", ex)
                except Exception as exc:
                    logger.warning("[CDP] 路由处理异常: %s", exc)
                route.continue_()

            try:
                page.route("**/backend-api/payments/checkout", handle_route)
                logger.info("[CDP] 已注册 **/backend-api/payments/checkout 请求重写拦截器")
            except Exception as e:
                logger.warning("[CDP] 注册路由拦截器异常: %s", e)

        # 1. 注册网络请求监听器：自动嗅探 Stripe 公钥 (pk_live_...) 及全量支付交互
        def handle_request(request):
            nonlocal captured_pk
            try:
                u = request.url
                if "stripe.com" in u or "payment" in u or "checkout" in u:
                    import re
                    m = re.search(r'(pk_live_[a-zA-Z0-9]+)', u)
                    if not m and request.post_data:
                        m = re.search(r'(pk_live_[a-zA-Z0-9]+)', str(request.post_data))
                    if not m:
                        auth_h = request.headers.get("authorization", "")
                        m = re.search(r'(pk_live_[a-zA-Z0-9]+)', auth_h)
                    if m and not captured_pk:
                        captured_pk = m.group(1)
                        logger.info("[提链-Network] 捕获到 Stripe 公钥: %s…", captured_pk[:16])
                    logger.info("[提链-Network] 抓取到支付相关请求: %s %s", request.method, u[:120])
            except Exception:
                pass

        # 2. 注册网络响应与页面导航监听器
        def handle_response(response):
            nonlocal stripe_url, checkout_response_data, checkout_session_id, captured_pk
            try:
                url = response.url
                if "/backend-api/payments/checkout" in url:
                    try:
                        data = response.json()
                        checkout_response_data = data
                        logger.info("[提链-Network] 拦截到 /payments/checkout 响应: status=%s, payload=%s", response.status, json.dumps(data))
                        if isinstance(data, dict):
                            cs = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
                            if cs:
                                checkout_session_id = str(cs)
                                logger.info("[提链-Network] 成功捕获结账会话 ID: %s", cs)
                            target = data.get("url")
                            if target and isinstance(target, str) and target.startswith("http"):
                                stripe_url = target
                                _emit(f"拦截到官方 Stripe 结账长链: {stripe_url}")
                                logger.info("[提链-Network] 成功截获原生 Stripe 长链: %s", stripe_url)
                    except Exception as exc:
                        logger.warning("[提链-Network] 解析 checkout 响应失败: %s", exc)
                elif "checkout.stripe.com" in url or "pay.openai.com" in url:
                    if url.startswith("http"):
                        logger.info("[提链-Network] 观察到 Stripe 页面响应: %s", url)
                        if not stripe_url or "#" not in stripe_url:
                            stripe_url = url
            except Exception:
                pass

        def handle_framenavigated(frame):
            nonlocal stripe_url
            try:
                url = frame.url
                if ("checkout.stripe.com" in url or "pay.openai.com" in url) and url.startswith("http"):
                    logger.info("[提链-Frame] 页面已导航到 Stripe 结账台: %s", url)
                    stripe_url = url
            except Exception:
                pass

        page.on("request", handle_request)
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
            const gotIt = btns.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return /^(got it|了解|閉じる|dismiss|close|đóng|bỏ qua)$/i.test(t);
            });
            if (gotIt && (gotIt.offsetWidth || gotIt.offsetHeight)) {
                gotIt.click();
            }
        """)
        time.sleep(1.5)
    except Exception:
        pass

    # 3. 定义精准定位函数：弹窗内试用确认动作按钮 vs 外部唤起入口按钮
    def _find_modal_action_btn():
        """
        在已弹出的定价弹窗中定位真实的 Plus 试用提交/确认按钮。
        关键区分：
        - 弹窗内动作按钮：【特別オファーを利用する】/【Dùng thử ưu đãi đặc biệt】/【Claim special offer】
        - 外部侧边栏入口：【オファーを受け取る】/【Nhận ưu đãi】/【Claim offer】
        坚决排除 Free / Go / Pro 套餐按钮，且排除外部“受け取る/nhận”类入口按钮。
        """
        return driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || (el.getClientRects && el.getClientRects().length));
            const allButtons = [...document.querySelectorAll('button, div[role="button"], a[role="button"]')].filter(visible);

            // 1. 优先在 dialog / modal 弹窗容器内寻找
            const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], [data-state="open"], .modal')].filter(visible);
            for (const dialog of dialogs) {
                const dialogBtns = [...dialog.querySelectorAll('button, div[role="button"], a[role="button"]')].filter(visible);
                const btn = dialogBtns.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (/閉じる|close|cancel|hủy|bỏ qua/i.test(t)) return false;
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン') || t.includes('current plan')) return false;
                    if (/(?:^|\s)(?:go|pro|team|business|enterprise)(?:\s|$)/.test(t) && !t.includes('plus')) return false;

                    // 精准动作关键词 (JP/VN/EN)
                    return /特別オファーを利用|オファーを利用|特典を利用|利用する|plus を試す|無料で試す|plus をはじめる|plus にアップグレード|plus を利用|dùng thử ưu đãi đặc biệt|dùng thử plus|ưu đãi đặc biệt|claim special offer|try special offer|try plus|start trial|claim offer/i.test(t);
                }) || dialogBtns.find(b => {
                    // 弹窗内 ChatGPT Plus 卡片内部的按钮
                    const card = b.closest('div, section');
                    const cardText = card ? (card.innerText || '').toLowerCase() : '';
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン')) return false;
                    return (cardText.includes('chatgpt plus') || cardText.includes('plus')) &&
                           /利用|dùng thử|try|claim|start|get|はじめる|アップグレード/i.test(t);
                }) || dialogBtns.find(b => {
                    // 弹窗内主要蓝色/高亮按钮 (非当前套餐和关闭)
                    const style = window.getComputedStyle(b);
                    const bg = style.backgroundColor || '';
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン')) return false;
                    const isBlue = bg.includes('37, 99, 235') || bg.includes('16, 163, 127') || (bg.includes('rgb(') && !bg.includes('255, 255, 255') && !bg.includes('0, 0, 0'));
                    return isBlue && /オファー|特典|plus|ưu đãi|trial|offer|はじめる/i.test(t);
                });

                if (btn) {
                    btn.scrollIntoView({ block: 'center' });
                    try { btn.click(); } catch (_) {}
                    const r = btn.getBoundingClientRect();
                    return {
                        ok: true,
                        in_dialog: true,
                        text: btn.innerText.trim(),
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2
                    };
                }
            }

            // 2. 若无 dialog 容器标示，全局检索具有明确提交语义的按钮 (严格排除单纯的“受け取る/nhận”侧边栏入口)
            const globalBtn = allButtons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン') || t.includes('current plan')) return false;
                if (/閉じる|close|cancel|hủy|bỏ qua/i.test(t)) return false;
                // 重点：必须是“利用/Dùng thử/Try/Special offer”，排除纯侧栏入口
                return /特別オファーを利用|オファーを利用|特典を利用|plus を試す|無料で試す|plus をはじめる|plus にアップグレード|plus を利用|dùng thử ưu đãi đặc biệt|dùng thử plus|claim special offer|try special offer/i.test(t);
            });

            if (globalBtn) {
                globalBtn.scrollIntoView({ block: 'center' });
                try { globalBtn.click(); } catch (_) {}
                const r = globalBtn.getBoundingClientRect();
                return {
                    ok: true,
                    in_dialog: false,
                    text: globalBtn.innerText.trim(),
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2
                };
            }

            return { ok: false, all_buttons: allButtons.map(b => b.innerText.trim()).filter(Boolean) };
        """)

    def _find_pricing_entry_btn():
        """
        在页面主界面/侧边栏中定位唤出定价弹窗的入口按钮。
        例如侧边栏的「オファーを受け取る」/「Nhận ưu đãi」/「アップグレード」/「Upgrade」，以及右上角的「無料オファー」。
        """
        return driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const allButtons = [...document.querySelectorAll('button, a, div[role="button"]')].filter(visible);

            // 1. 匹配顶部横幅/侧边栏/专属优惠入口 (中英日越)
            const entryBtn = allButtons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('login') || t.includes('signin') || t.includes('lên go') || t.includes('lên pro')) return false;
                return (
                    t.includes('無料オファー') ||
                    t.includes('オファーを受け取る') ||
                    t.includes('特典を受け取る') ||
                    t.includes('特別オファー') ||
                    t.includes('オファー') ||
                    t.includes('特典') ||
                    t.includes('nhận ưu đãi') ||
                    t.includes('claim offer') ||
                    t.includes('get offer') ||
                    t.includes('ưu đãi đặc biệt') ||
                    t.includes('plus にアップグレード') ||
                    t.includes('upgrade to plus') ||
                    t.includes('nâng cấp lên plus') ||
                    t.includes('アップグレード') ||
                    t.includes('upgrade') ||
                    t.includes('nâng cấp')
                );
            });

            if (entryBtn) {
                entryBtn.scrollIntoView({ block: 'center' });
                try { entryBtn.click(); } catch (_) {}
                const r = entryBtn.getBoundingClientRect();
                return { ok: true, text: entryBtn.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }

            // 2. 选择器备用匹配
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
                    try { el.click(); } catch (_) {}
                    const r = el.getBoundingClientRect();
                    return { ok: true, text: el.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
                }
            }
            return { ok: false };
        """)

    # 4. 阶段一：等待并确认定价弹窗是否已展开
    _emit("等待 ChatGPT 渲染定价与优惠弹窗…")
    btn_info = {"ok": False}
    t_find_end = time.time() + 15.0
    while time.time() < t_find_end:
        btn_info = _find_modal_action_btn()
        if btn_info.get("ok"):
            logger.info("[提链-拟人化] 定价弹窗已就绪，检测到确认按钮: %s", btn_info)
            break
        time.sleep(1.5)

    # 5. 阶段二：若弹窗未自动弹出，点击侧边栏 / 菜单「Claim offer / Upgrade」唤出弹窗
    if not btn_info.get("ok") and not stripe_url:
        _emit("未见直接弹窗，正在寻找侧边栏「Claim offer / Upgrade / オファー」入口…")
        entry_info = _find_pricing_entry_btn()
        logger.info("[提链-拟人化] 定位侧边栏升级/优惠入口: %s", entry_info)
        if entry_info.get("ok") and entry_info.get("x") and entry_info.get("y"):
            _emit(f"点击侧边栏入口【{entry_info.get('text')}】唤出定价弹窗…")
            x = float(entry_info["x"])
            y = float(entry_info["y"])
            if page and hasattr(page, "mouse") and x > 0 and y > 0:
                page.mouse.move(x, y)
                time.sleep(0.08)
                page.mouse.down()
                time.sleep(0.06)
                page.mouse.up()
            time.sleep(3.0)

            # 点击侧边栏后，循环等待弹窗及确认按钮渲染
            t_modal_wait = time.time() + 20.0
            while time.time() < t_modal_wait:
                btn_info = _find_modal_action_btn()
                if btn_info.get("ok"):
                    logger.info("[提链-拟人化] 点击侧边栏入口后成功唤出弹窗，锁定确认按钮: %s", btn_info)
                    break
                time.sleep(1.5)

    if stripe_url:
        return {"ok": True, "url": stripe_url, "checkout_session_id": stripe_url.split("/")[-1]}

    # 6. 阶段三：若仍未弹出，尝试左下角个人信息/用户菜单
    if not btn_info.get("ok") and not stripe_url:
        _emit("未见直接弹窗，正在展开左下角账户菜单以触发升级入口…")
        profile_btn_info = driver.execute_script("""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const profileBtn = document.querySelector('button[data-testid="profile-button"], [data-testid="accounts-profile-button"], div[data-testid="user-menu"], button[aria-label*="User"], button[aria-label*="Profile"]') ||
                [...document.querySelectorAll('button, div[role="button"]')].find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return (t.includes('free') || t.includes('plus')) && visible(el);
                });
            if (profileBtn) {
                profileBtn.scrollIntoView({ block: 'center' });
                const r = profileBtn.getBoundingClientRect();
                return { ok: true, text: profileBtn.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }
            return { ok: false };
        """)
        logger.info("[提链-拟人化] 左下角账户按钮定位: %s", profile_btn_info)
        if profile_btn_info and profile_btn_info.get("ok") and profile_btn_info.get("x"):
            if page and hasattr(page, "mouse"):
                page.mouse.move(float(profile_btn_info["x"]), float(profile_btn_info["y"]))
                time.sleep(0.08)
                page.mouse.down()
                time.sleep(0.06)
                page.mouse.up()
            time.sleep(1.5)

            menu_info = driver.execute_script("""
                const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
                const menuItems = [...document.querySelectorAll('[role="menuitem"], div[role="button"], button, a')].filter(visible);
                const upgradeItem = menuItems.find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return (
                        t.includes('nâng cấp') ||
                        t.includes('upgrade') ||
                        t.includes('claim') ||
                        t.includes('ưu đãi') ||
                        t.includes('plus') ||
                        t.includes('特典') ||
                        t.includes('オファー') ||
                        t.includes('アップグレード')
                    ) && !t.includes('free') && !t.includes('login') && !t.includes('logout') && !t.includes('đăng xuất');
                });
                if (upgradeItem) {
                    upgradeItem.scrollIntoView({ block: 'center' });
                    try { upgradeItem.click(); } catch (_) {}
                    const r = upgradeItem.getBoundingClientRect();
                    return { ok: true, text: upgradeItem.innerText.trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
                }
                return { ok: false, items: menuItems.map(m => m.innerText.trim()).filter(Boolean) };
            """)
            logger.info("[提链-拟人化] 个人菜单内升级项定位: %s", menu_info)
            if menu_info and menu_info.get("ok") and menu_info.get("x"):
                if page and hasattr(page, "mouse"):
                    page.mouse.move(float(menu_info["x"]), float(menu_info["y"]))
                    time.sleep(0.08)
                    page.mouse.down()
                    time.sleep(0.06)
                    page.mouse.up()
            time.sleep(3.0)

            t_menu_wait = time.time() + 15.0
            while time.time() < t_menu_wait:
                btn_info = _find_modal_action_btn()
                if btn_info.get("ok"):
                    break
                time.sleep(1.5)

    if stripe_url:
        return {"ok": True, "url": stripe_url, "checkout_session_id": stripe_url.split("/")[-1]}

    # 7. 阶段四：定位到试用确认按钮，执行真实鼠标轨迹点击
    if not btn_info.get("ok"):
        btn_info = _find_modal_action_btn()

    if not btn_info.get("ok"):
        try:
            from pathlib import Path
            screenshots_dir = Path("/app/注册日志/screenshots")
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            shot_path = screenshots_dir / f"extract_no_btn_{int(time.time())}.png"
            driver.save_screenshot(str(shot_path))
            logger.info("[提链-拟人化] 未能定位试用按钮，现场快照已保存至 %s", shot_path)
        except Exception:
            pass
        return {"ok": False, "error": f"未能定位到 Plus 试用确认按钮，当前按钮: {btn_info.get('all_buttons')}"}

    _emit(f"已锁定 Plus 试用确认按钮【{btn_info.get('text')}】，正在模拟真实鼠标点击…")
    x = float(btn_info["x"])
    y = float(btn_info["y"])
    logger.info("[提链-拟人化] 执行真实鼠标轨迹点击试用确认按钮 '%s' at (%s, %s)", btn_info.get("text"), x, y)
    if page and hasattr(page, "mouse") and x > 0 and y > 0:
        try:
            page.mouse.move(x, y)
            time.sleep(0.12)
            page.mouse.down()
            time.sleep(0.08)
            page.mouse.up()
        except Exception as exc:
            logger.warning("[提链-拟人化] Playwright mouse 点击异常: %s", exc)

    driver.execute_script(r"""
        try {
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || (el.getClientRects && el.getClientRects().length));
            const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], [data-state="open"], .modal')].filter(visible);
            let target = null;
            for (const dialog of dialogs) {
                const btns = [...dialog.querySelectorAll('button, div[role="button"], a[role="button"]')].filter(visible);
                target = btns.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (/閉じる|close|cancel|hủy|bỏ qua/i.test(t)) return false;
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン') || t.includes('current plan')) return false;
                    return /特別オファーを利用|オファーを利用|特典を利用|利用する|plus を試す|無料で試す|dùng thử ưu đãi đặc biệt|dùng thử plus|claim special offer|try special offer|try plus|start trial|claim offer/i.test(t);
                });
                if (target) break;
            }
            if (!target) {
                const allButtons = [...document.querySelectorAll('button, div[role="button"], a[role="button"]')].filter(visible);
                target = allButtons.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中的プラン')) return false;
                    if (/閉じる|close|cancel/i.test(t)) return false;
                    return /特別オファーを利用|オファーを利用|特典を利用|plus を試す|無料で試す|dùng thử ưu đãi đặc biệt|claim special offer/i.test(t);
                });
            }
            if (target) {
                target.focus();
                target.click();
            }
        } catch (_) {}
    """)

    # 8. 阶段五：等待捕获 Stripe Checkout 链接 (Sentinel PoW 计算需 30~80s)
    _emit("等待官方生成 Stripe 结账链接 (含 Sentinel 人机对抗计算，最长等待 120 秒)…")
    logger.info("[提链-拟人化] 开始等待 Stripe 链接生成 (最长 120s)…")
    wait_start = time.time()
    reclick_attempted = False
    while time.time() - wait_start < timeout:
        cur = str(getattr(driver, "current_url", "") or "")
        if ("checkout.stripe.com" in cur or "pay.openai.com" in cur) and ("#" in cur or "cs_" in cur):
            stripe_url = cur
            break
        if stripe_url and ("#" in stripe_url or "cs_" in stripe_url):
            if ("checkout.stripe.com" in cur or "pay.openai.com" in cur) and "#" in cur:
                stripe_url = cur
            break
        if checkout_session_id and (captured_pk or (time.time() - wait_start > 5.0)):
            logger.info("[提链-拟人化] 成功提前捕获 checkout_session_id (%s)，提前退出等待", checkout_session_id)
            break

        # 兜底：若 10 秒后未见任何网络请求或跳转且按钮仍可点击，再次触发双重点击
        if not reclick_attempted and (time.time() - wait_start > 10.0) and not checkout_response_data:
            check_again = _find_modal_action_btn()
            if check_again.get("ok"):
                logger.info("[提链-拟人化] 首次点击可能未被触发，执行多重补点击确认按钮...")
                _emit("正在确保试用确认点击已触发…")
                if page and check_again.get("x") and hasattr(page, "mouse"):
                    try:
                        page.mouse.move(float(check_again["x"]) + 2, float(check_again["y"]) + 2)
                        time.sleep(0.1)
                        page.mouse.down()
                        time.sleep(0.08)
                        page.mouse.up()
                    except Exception:
                        pass
                if page:
                    try:
                        loc = page.locator('button:has-text("特別オファーを利用"), button:has-text("オファーを利用")').first
                        if loc.is_visible():
                            loc.click(timeout=3000)
                    except Exception:
                        pass
                driver.execute_script(r"""
                    try {
                        const btns = [...document.querySelectorAll('button')];
                        const b = btns.find(el => /特別オファーを利用|オファーを利用/i.test(el.innerText || ''));
                        if (b) { b.focus(); b.click(); }
                    } catch (_) {}
                """)
            reclick_attempted = True

        time.sleep(1.0)

    # 兜底再次检查当前 URL 与页面上下文中的 Stripe 信息
    cur = str(getattr(driver, "current_url", "") or "")
    if ("checkout.stripe.com" in cur or "pay.openai.com" in cur) and ("#" in cur or "cs_" in cur):
        stripe_url = cur

    if not captured_pk:
        try:
            dom_pk = driver.execute_script("""
                const m = (document.documentElement ? document.documentElement.innerHTML : '').match(/pk_live_[a-zA-Z0-9]+/);
                if (m) return m[0];
                for (const s of document.scripts || []) {
                    if (s.src && s.src.includes('stripe')) {
                        try {
                            const q = new URL(s.src).searchParams.get('key');
                            if (q) return q;
                        } catch (_) {}
                    }
                }
                return null;
            """)
            if dom_pk:
                captured_pk = str(dom_pk)
                logger.info("[提链-DOM] 从页面 DOM 中提取到 Stripe 公钥: %s", captured_pk[:16])
        except Exception:
            pass

    def _format_checkout_url(cs_id: str, processor_entity: str = "", origin_country: str = "") -> str:
        cs = str(cs_id or "").strip()
        if cs.startswith("oaics_"):
            entity = processor_entity or ("openai_llc" if (origin_country or "").upper() == "US" else "openai_ie")
            return f"https://chatgpt.com/checkout/{entity}/{cs}"
        return f"https://checkout.stripe.com/c/pay/{cs}"


    if stripe_url:
        cs_id = stripe_url.split("/")[-1].split("#")[0]
        _emit("🎉 官方 Stripe 试用结账长链提取成功！")
        logger.info("[提链-拟人化] 🎉 成功捕获 Stripe 结账长链: %s", stripe_url)
        return {
            "ok": True,
            "url": stripe_url,
            "checkout_session_id": cs_id,
            "api_key": captured_pk or (checkout_response_data or {}).get("publishable_key"),
            "customer_session_client_secret": (checkout_response_data or {}).get("customer_session_client_secret"),
            "client_secret": (checkout_response_data or {}).get("client_secret"),
            "confirm_return_url": (checkout_response_data or {}).get("confirm_return_url"),
            "checkout_data": checkout_response_data,
        }

    if checkout_session_id:
        _emit(f"成功捕获结账会话 ID: {checkout_session_id}")
        logger.info("[提链-拟人化] 成功捕获结账会话 ID: %s (api_key=%s)", checkout_session_id, captured_pk)
        entity = str((checkout_response_data or {}).get("processor_entity") or "").strip()
        chk_url = _format_checkout_url(checkout_session_id, entity, origin_country)
        return {
            "ok": True,
            "checkout_session_id": checkout_session_id,
            "url": chk_url,
            "processor_entity": entity or ("openai_llc" if origin_country == "US" else "openai_ie"),
            "api_key": captured_pk or (checkout_response_data or {}).get("publishable_key"),
            "customer_session_client_secret": (checkout_response_data or {}).get("customer_session_client_secret"),
            "client_secret": (checkout_response_data or {}).get("client_secret"),
            "confirm_return_url": (checkout_response_data or {}).get("confirm_return_url"),
            "checkout_data": checkout_response_data,
        }

    if checkout_response_data and isinstance(checkout_response_data, dict):
        err_msg = str(checkout_response_data.get("error") or checkout_response_data.get("detail") or "")
        if "already" in err_msg.lower() or "active_subscription" in err_msg.lower():
            return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}
        cs_id = checkout_response_data.get("checkout_session_id") or checkout_response_data.get("session_id")
        if cs_id:
            entity = str(checkout_response_data.get("processor_entity") or "").strip()
            chk_url = _format_checkout_url(str(cs_id), entity, origin_country)
            return {
                "ok": True,
                "checkout_session_id": str(cs_id),
                "url": chk_url,
                "processor_entity": entity or ("openai_llc" if origin_country == "US" else "openai_ie"),
                "api_key": captured_pk,
            }

    try:
        from pathlib import Path
        screenshots_dir = Path("/app/注册日志/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        shot_path = screenshots_dir / f"extract_fail_{int(time.time())}.png"
        driver.save_screenshot(str(shot_path))
        logger.info("[提链-拟人化] 未能出链，现场快照已保存至 %s", shot_path)
    except Exception:
        pass

    logger.warning("[提链-拟人化] 未能通过拟人化操作捕获到链接，当前 URL: %s, 响应: %s", getattr(driver, "current_url", ""), checkout_response_data)
    return {"ok": False, "error": "未能通过拟人化操作捕获到 Stripe 结账长链"}


def _execute_js_checkout(
    driver: Any,
    access_token: str,
    account_id: str,
    country: str,
    currency: str,
    promo_campaign_id: str = "",
) -> dict[str, Any]:
    """在当前已建立好边缘/盾环境的浏览器中，通过真实前端上下文发起原生 checkout 请求 (hosted 模式)。"""
    cur_url = str(getattr(driver, "current_url", "") or "")
    if "chatgpt.com" not in cur_url:
        try:
            driver.get("https://chatgpt.com/")
            time.sleep(2.0)
        except Exception:
            pass

    clean_token = (access_token or "").strip()
    if clean_token.lower().startswith("bearer "):
        clean_token = clean_token[7:].strip()

    res = None
    page = getattr(driver, "page", None)
    if page is not None and not type(driver).__name__.startswith("MagicMock"):
        js_evaluate = """
        async ([cleanToken, accountId, country, currency, promoCampaignId]) => {
            const body = {
                entry_point: 'all_plans_pricing_modal',
                plan_name: 'chatgptplusplan',
                checkout_ui_mode: 'hosted',
                billing_details: {
                    country: (country || 'JP').toUpperCase(),
                    currency: (currency || 'USD').toUpperCase()
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
                'Authorization': 'Bearer ' + cleanToken
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

            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            try {
                const r = await fetch('https://chatgpt.com/backend-api/payments/checkout', {
                    method: 'POST',
                    credentials: 'include',
                    headers: headers,
                    body: JSON.stringify(body),
                    signal: controller.signal
                });
                clearTimeout(timer);
                let data = {};
                try { data = await r.json(); } catch(e) {}
                return { status: r.status, ok: r.ok, data: data };
            } catch (err) {
                clearTimeout(timer);
                return { ok: false, error: String(err) };
            }
        }
        """
        try:
            res = page.evaluate(js_evaluate, [clean_token, str(account_id or ""), country, currency, promo_campaign_id])
        except Exception as exc:
            logger.warning(f"[提链] page.evaluate 原生 checkout 异常: {exc}")
            res = None

    if res is None:
        js_checkout = """
        const done = (typeof __cloak_done === 'function') ? __cloak_done : arguments[arguments.length - 1];
        const token = arguments[0];
        const accountId = arguments[1];
        const country = (arguments[2] || 'JP').toUpperCase();
        const currency = (arguments[3] || 'USD').toUpperCase();
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

        const cleanToken = (token || '').replace(/^Bearer\\s+/i, '').trim();
        const headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + cleanToken
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

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 8000);
        fetch('https://chatgpt.com/backend-api/payments/checkout', {
            method: 'POST',
            credentials: 'include',
            headers: headers,
            body: JSON.stringify(body),
            signal: controller.signal
        })
        .then(async r => {
            clearTimeout(timer);
            let data = {};
            try { data = await r.json(); } catch(e) {}
            if (typeof done === 'function') done({ status: r.status, ok: r.ok, data: data });
        })
        .catch(err => {
            clearTimeout(timer);
            if (typeof done === 'function') done({ ok: false, error: String(err) });
        });
        """
        try:
            res = driver.execute_async_script(js_checkout, clean_token, str(account_id or ""), country, currency, promo_campaign_id)
        except Exception as exc:
            logger.warning(f"[提链] execute_async_script 异常: {exc}")
            res = {"ok": False, "error": str(exc)}

    if not res or not isinstance(res, dict):
        return {"ok": False, "error": "JS checkout returned invalid response"}

    checkout_data = res.get("data") if isinstance(res.get("data"), dict) else {}
    url = (
        checkout_data.get("url")
        or checkout_data.get("stripe_hosted_url")
        or checkout_data.get("checkout_url")
    )
    cs_id = (
        checkout_data.get("checkout_session_id")
        or checkout_data.get("session_id")
        or checkout_data.get("id")
    )
    entity = str(checkout_data.get("processor_entity") or ("openai_llc" if country == "US" else "openai_ie")).strip()
    api_key = (
        checkout_data.get("api_key")
        or checkout_data.get("publishable_key")
        or checkout_data.get("public_key")
    )

    if not cs_id and url:
        if "/c/pay/" in url:
            cs_id = url.split("/c/pay/")[-1].split("?")[0].split("#")[0]
        elif "/checkout/" in url:
            cs_id = url.rstrip("/").split("/")[-1]

    if not url and cs_id:
        if str(cs_id).startswith("oaics_"):
            url = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
        else:
            url = f"https://checkout.stripe.com/c/pay/{cs_id}"

    if url:
        return {
            "ok": True,
            "url": url,
            "checkout_session_id": cs_id,
            "processor_entity": entity,
            "api_key": api_key,
            "customer_session_client_secret": checkout_data.get("customer_session_client_secret"),
            "client_secret": checkout_data.get("client_secret"),
            "confirm_return_url": checkout_data.get("confirm_return_url"),
            "checkout_data": checkout_data,
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
    target_lpm: str = "ideal",
    log_cb: Any = None,
) -> dict[str, Any]:
    """
    使用 CloakBrowser 指纹浏览器真实环境自动化获取官方 Stripe 结账会话并提取原生本地支付直链 (LPM)。
    优先使用存量有效 Token 进行会话直通获取；若 Token 缺失或失效，无缝走浏览器登录自愈流。
    """
    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    origin_country = str(account.get("country_code") or "JP").strip().upper() or "JP"
    currency = get_currency_for_country(origin_country)

    lpm = str(target_lpm or "ideal").strip().lower()
    from core.stripe_lpm_engine import LPM_SPECS
    if lpm in LPM_SPECS:
        req_country = LPM_SPECS[lpm]["country"]
        req_currency = LPM_SPECS[lpm]["currency"].upper()
    else:
        req_country = origin_country
        req_currency = currency

    def _convert_to_lpm_if_needed(res: dict) -> dict:
        if not res or not res.get("ok") or res.get("already_paid"):
            return res
        cs_id = str(res.get("checkout_session_id") or "").strip()
        raw_url = str(res.get("url") or "").strip()
        if not cs_id and "/c/pay/" in raw_url:
            cs_id = raw_url.split("/c/pay/")[-1].split("?")[0].split("#")[0]
        elif not cs_id and "/checkout/" in raw_url:
            cs_id = raw_url.rstrip("/").split("/")[-1]

        entity = res.get("processor_entity") or ("openai_llc" if origin_country == "US" else "openai_ie")
        short_url = f"https://chatgpt.com/checkout/{entity}/{cs_id}" if cs_id else ""
        long_url = raw_url if ("/c/pay/" in raw_url or "checkout.stripe.com" in raw_url) else (f"https://checkout.stripe.com/c/pay/{cs_id}" if cs_id and cs_id.startswith("cs_") else "")
        lpm_url = ""

        # 针对 oaics_* 原生站内结账会话
        if cs_id.startswith("oaics_") or "oaics_" in raw_url:
            short_url = short_url or f"https://chatgpt.com/checkout/{entity}/{cs_id}"
            bridge_url = f"/pay/checkout/{cs_id}"
            _emit("检测到 OpenAI 原生特惠结账会话 (oaics_*)，已生成免登独立收银长链 (形态 A) 与站内短链")
            if lpm and lpm not in ("card", "direct", "none", "stripe", "hosted"):
                _emit("提示：特惠试用资格官方限定绑卡/Link 签约 (无额度扣除)，已为您生成免登独立收银长链")
            logger.info("[提链] 识别到 oaics_* 原生结账会话: %s -> bridge: %s, short: %s", cs_id, bridge_url, short_url)
            return {
                "ok": True,
                "url": bridge_url,
                "short_url": short_url,
                "long_url": bridge_url,
                "lpm_url": "",
                "checkout_session_id": cs_id,
                "processor_entity": entity,
                "type": "checkout_bridge",
                "name": "ChatGPT 官方免登独立收银长链",
                "api_key": res.get("api_key"),
                "customer_session_client_secret": res.get("customer_session_client_secret"),
                "client_secret": res.get("client_secret"),
                "confirm_return_url": res.get("confirm_return_url"),
                "checkout_data": res.get("checkout_data"),
            }

        # 针对标准 Stripe cs_* 会话且要求三方支付 (LPM)
        if lpm and lpm not in ("card", "direct", "none", "stripe", "hosted") and cs_id.startswith("cs_"):
            _emit(f"已捕获 Stripe 结账会话 ({cs_id[:16]}…)，正在调用 Stripe LPM 引擎提取【{lpm.upper()}】原生支付直链…")
            from core.stripe_lpm_engine import StripeLPMExtractor
            try:
                extractor = StripeLPMExtractor(
                    session_url_or_id=cs_id,
                    target_lpm=lpm,
                    proxy=proxy_url,
                    api_key=res.get("api_key"),
                )
                lpm_res = extractor.run()
                if lpm_res.get("ok"):
                    _emit(f"🎉 成功生成【{lpm.upper()}】原生第三方支付跳转直链！")
                    lpm_res["short_url"] = short_url
                    lpm_res["long_url"] = long_url or f"https://checkout.stripe.com/c/pay/{cs_id}"
                    lpm_res["lpm_url"] = lpm_res.get("url")
                    lpm_res["customer_session_client_secret"] = res.get("customer_session_client_secret")
                    lpm_res["checkout_data"] = res.get("checkout_data")
                    return lpm_res
                _emit(f"Stripe LPM 引擎提取未直接完成 ({lpm_res.get('error')})，降级为 Stripe 原生收银台免登长链…")
            except Exception as e:
                _emit(f"Stripe LPM 引擎提取异常 ({e})，降级为 Stripe 原生收银台免登长链…")
                logger.warning("[提链] Stripe LPM 引擎提取异常: %s", e)

            # 降级返回原生 Stripe 托管免登长链
            hosted_url = long_url or f"https://checkout.stripe.com/c/pay/{cs_id}"
            return {
                "ok": True,
                "url": hosted_url,
                "short_url": short_url,
                "long_url": hosted_url,
                "lpm_url": "",
                "checkout_session_id": cs_id,
                "processor_entity": res.get("processor_entity") or "openai_ie",
                "type": "stripe_hosted",
                "name": f"Stripe 官方托管免登长链 (支持 {lpm.upper()})",
                "api_key": res.get("api_key"),
                "customer_session_client_secret": res.get("customer_session_client_secret"),
                "client_secret": res.get("client_secret"),
                "confirm_return_url": res.get("confirm_return_url"),
                "checkout_data": res.get("checkout_data"),
            }

        # 针对标准 Stripe cs_* 会话 (免登长链)
        if cs_id.startswith("cs_"):
            stripe_url = long_url or f"https://checkout.stripe.com/c/pay/{cs_id}"
            _emit("已捕获 Stripe 官方托管免登长链！")
            return {
                "ok": True,
                "url": stripe_url,
                "short_url": short_url,
                "long_url": stripe_url,
                "lpm_url": "",
                "checkout_session_id": cs_id,
                "processor_entity": res.get("processor_entity") or "openai_ie",
                "type": "stripe_hosted",
                "name": "Stripe 官方托管免登长链",
                "api_key": res.get("api_key"),
                "customer_session_client_secret": res.get("customer_session_client_secret"),
                "client_secret": res.get("client_secret"),
                "confirm_return_url": res.get("confirm_return_url"),
                "checkout_data": res.get("checkout_data"),
            }

        res["short_url"] = short_url
        res["long_url"] = long_url
        res["lpm_url"] = ""
        return res

    def _do_checkout(tok: str, acc_id: str, phase_desc: str, allow_human: bool = True) -> dict[str, Any]:
        _emit(f"{phase_desc}，正在向 OpenAI 发起【{req_country} ({lpm.upper()})】原生结账申请 (hosted 模式)…")

        # 1. 优先尝试以目标国家 + hosted 模式申请 (若有试用活动先带试用活动)
        chk_res = _execute_js_checkout(driver, tok, acc_id, req_country, req_currency, promo_campaign_id=promo_campaign_id)

        # 2. 若带 promo 失败且非 401，尝试不带 promo 的常规 Plus 申请
        if not chk_res.get("ok") and promo_campaign_id and chk_res.get("status") != 401:
            _emit("带活动申请未成功，自动切换至常规 Plus 套餐请求…")
            chk_res = _execute_js_checkout(driver, tok, acc_id, req_country, req_currency, promo_campaign_id="")

        # 3. 若针对目标国家失败且目标国家不是原属地，且非 401，尝试以原属地申请
        if not chk_res.get("ok") and req_country != origin_country and chk_res.get("status") != 401:
            _emit(f"目标属地申请未成功，尝试原属地【{origin_country}】免登长链申请…")
            chk_res = _execute_js_checkout(driver, tok, acc_id, origin_country, currency, promo_campaign_id=promo_campaign_id)
            if not chk_res.get("ok") and promo_campaign_id:
                chk_res = _execute_js_checkout(driver, tok, acc_id, origin_country, currency, promo_campaign_id="")

        # 4. 若接口成功出链或已是 Plus，进行转换与返回
        if chk_res.get("ok"):
            return _convert_to_lpm_if_needed(chk_res)
        if chk_res.get("already_paid"):
            return chk_res

        # 5. 若接口方式未成功且非 401，且允许拟人化 (已处于登录环境中)，最后尝试拟人化 UI 模拟点击兜底
        if allow_human and chk_res.get("status") != 401:
            _emit("接口请求未直接出链，尝试通过拟人化 UI 操作唤起…")
            human_res = _human_extract_checkout_url(driver, promo_campaign_id=promo_campaign_id, emit_fn=_emit, timeout=60.0, origin_country=origin_country)
            if human_res.get("ok"):
                return _convert_to_lpm_if_needed(human_res)
            if human_res.get("already_paid"):
                return human_res

        return chk_res

    email = str(account.get("email") or "").strip()
    if not email:
        raise ValueError("账号缺少邮箱信息，无法通过指纹浏览器提链")

    account_id_db = account.get("id")
    access_token = str(account.get("access_token") or "").strip()
    account_id = str(account.get("account_id") or "").strip()
    totp_secret = str(account.get("totp_secret") or "").strip()
    if not totp_secret and account.get("extra_json"):
        try:
            extra = json.loads(account["extra_json"]) if isinstance(account["extra_json"], str) else account["extra_json"]
            totp_secret = str(extra.get("totp_secret") or extra.get("totp_key") or "").strip()
        except Exception:
            pass
    promo_campaign_id = str(
        account.get("plus_trial_campaign_id")
        or account.get("promo_campaign_id")
        or ""
    ).strip()

    from core.db import _extract_registration_password
    password = str(
        account.get("password")
        or _extract_registration_password(account)
        or account.get("registration_password")
        or account.get("account_password")
        or account.get("openai_password")
        or ""
    ).strip()

    # 本地校验 JWT 是否已过期
    token_expired = bool(account.get("token_expired") or is_jwt_expired(access_token))

    _emit("启动 CloakBrowser 原生指纹浏览器 (提链环境)…")
    from core.cloakbrowser_driver import build_cloak_driver

    driver = None
    try:
        driver, _ = build_cloak_driver(proxy=proxy_url)
        driver.set_page_load_timeout(90)

        # -------------------------------------------------------------
        # 阶段一：会话直通（若存在存量未过期 access_token）
        # -------------------------------------------------------------
        if access_token and not token_expired:
            _emit("检测到存量会话凭证，正在打开 ChatGPT 建立指纹与边缘环境…")
            try:
                driver.get("https://chatgpt.com/")
            except Exception as e:
                logger.warning("访问 chatgpt.com 发生警告: %s", e)
            time.sleep(2.0)
            solve_cloudflare_challenge_if_present(driver, max_wait=15.0, emit_fn=_emit)

            # 优先尝试直接使用存量 access_token 发起结账申请 (秒级直通)
            _emit("正在使用存量授权凭证快速发起结账会话…")
            chk_res = _do_checkout(access_token, account_id, "存量会话直通", allow_human=False)
            if chk_res.get("ok"):
                return chk_res
            if chk_res.get("already_paid"):
                return chk_res

            # 若存量凭证未直接出链，检查浏览器当前是否持有新鲜 session
            session_data = None
            cur_check = str(getattr(driver, "current_url", "") or "")
            if "chatgpt.com" in cur_check or "mock" in cur_check.lower():
                try:
                    session_data = _read_chatgpt_session_once(driver)
                except Exception:
                    pass

            if session_data and session_data.get("accessToken"):
                cur_tok = session_data.get("accessToken")
                cur_acc = (session_data.get("account") or {}).get("id") or account_id
                if cur_tok != access_token:
                    chk_res = _do_checkout(cur_tok, cur_acc, "浏览器存量会话有效")
                    if chk_res.get("ok"):
                        return chk_res
                    if chk_res.get("already_paid"):
                        return chk_res

            _emit("存量会话凭证已失效或未直接出链，切换至完整登录流程…")
            try:
                driver.delete_all_cookies()
                driver.execute_script("try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}")
            except Exception:
                pass
            access_token = ""

        # -------------------------------------------------------------
        # 阶段二：浏览器完整登录自愈流 (自愈登录状态机)
        # -------------------------------------------------------------
        if not access_token:
            email_source = account.get("email_service") or account.get("email_source")
            if not password and not email_source:
                raise RuntimeError(
                    f"账号 {email} 未设置密码，且注册时使用的临时邮箱已过期无可用接信通道，无法在全新浏览器中接收 OTP 验证码以建立网页登录态"
                )
            _emit(f"正在打开 ChatGPT 登录页以建立新会话 ({email})…")
            for attempt in range(3):
                try:
                    driver.get("https://chatgpt.com/auth/login")
                    break
                except Exception as e:
                    logger.warning("访问登录页超时/重试 (%d/3): %s", attempt + 1, e)
                    if attempt == 2:
                        raise e
                    time.sleep(2.0)
            time.sleep(2.5)

        otp_after_ts = time.time() - 2.0
        email_submitted = False
        otp_submitted = False
        last_otp_submit_ts = 0.0
        totp_attempts = 0
        max_totp_attempts = 3
        password_attempts = 0
        max_password_attempts = 3
        switch_pwdless_attempts = 0
        max_switch_pwdless_attempts = 3
        last_logged_url = ""
        last_totp_submit_time = 0.0

        t_end = time.time() + 240
        while time.time() < t_end:
            cur_url = str(driver.current_url or "")
            title = str(driver.title or "")
            base_url = cur_url.split("?")[0] if cur_url else ""

            if base_url and base_url != last_logged_url:
                last_logged_url = base_url
                _emit(f"页面流转: {base_url}")

            if "error" in cur_url and "rate_limit" in cur_url:
                raise RuntimeError("OpenAI 登录验证码发送过于频繁 (rate_limit_exceeded)，请稍后重试")

            # 0.5. 检测并恢复网络错误页 (chrome-error / neterror)
            if "chrome-error://" in cur_url or "about:neterror" in cur_url:
                _emit("检测到浏览器网络连接偶发异常 (chrome-error)，正在自动刷新恢复…")
                time.sleep(2.0)
                driver.refresh()
                time.sleep(3.0)
                continue

            # 1. 穿透 Cloudflare 质询
            if solve_cloudflare_challenge_if_present(driver, max_wait=15.0, emit_fn=_emit):
                time.sleep(1.0)
                continue

            # 1.5. 检测并恢复 OpenAI 认证错误页 (/auth/error / 問題が発生しました / Route Error / 500)
            is_auth_error = False
            if "/auth/error" in cur_url:
                is_auth_error = True
            else:
                try:
                    is_auth_error = driver.execute_script("""
                        const t = (document.body ? document.body.innerText : '').toLowerCase();
                        return t.includes('問題が発生しました') || t.includes('route error') || 
                               t.includes('500 internal server') || t.includes('不明なエラーが発生しました') ||
                               t.includes('something went wrong') || t.includes('there was a problem');
                    """)
                except Exception:
                    is_auth_error = False

            if is_auth_error:
                _emit("检测到处于 OpenAI 认证错误页 (/auth/error)，正在自动恢复并重试登录…")
                time.sleep(2.0)
                clicked_back = False
                try:
                    clicked_back = driver.execute_script("""
                        const btns = [...document.querySelectorAll('button, a')];
                        const btn = btns.find(b => {
                            const t = (b.innerText || '').trim().toLowerCase();
                            return /戻る|もう一度試す|try again|back|sign in|ログイン/i.test(t);
                        });
                        if (btn && (btn.offsetWidth || btn.offsetHeight)) {
                            btn.click();
                            return true;
                        }
                        return false;
                    """)
                except Exception:
                    clicked_back = False

                if not clicked_back:
                    try:
                        driver.delete_all_cookies()
                        driver.execute_script("try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}")
                    except Exception:
                        pass
                    driver.get("https://chatgpt.com/auth/login")

                email_submitted = False
                otp_submitted = False
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

            # 4. 提交账号邮箱步骤
            if not email_submitted:
                _emit("正在进入登录流程并提交账号邮箱…")
                try:
                    next_st = _submit_email_and_wait_next(driver, email, attempts=2, allow_login_password=True, timeout=45)
                    email_submitted = True
                    otp_after_ts = time.time() - 2.0
                    t_end = max(t_end, time.time() + 180)
                    logger.info("[提链] 邮箱提交完成，进入下一状态：%s", next_st)
                except Exception as exc:
                    logger.warning("[提链] 邮箱提交流程异常，继续轮询: %s", exc)
                continue

            # 5. 处于密码输入页面
            has_password_input = False
            try:
                has_password_input = driver.execute_script("""
                    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
                      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                    const pwd = document.querySelector('input[type="password"], input[name*="password" i], input[autocomplete="current-password"]');
                    return !!(pwd && visible(pwd));
                """)
            except Exception:
                has_password_input = False

            if has_password_input:
                has_pwd_error = False
                try:
                    has_pwd_error = driver.execute_script("""
                        const t = (document.body ? document.body.innerText : '').toLowerCase();
                        return t.includes('incorrect email address or password') || 
                               t.includes('パスワードが正しくありません') ||
                               t.includes('wrong password');
                    """)
                except Exception:
                    has_pwd_error = False

                # 若密码存在且未试过且页面无报错，尝试输入密码提交
                if password and password_attempts < 2 and not has_pwd_error:
                    password_attempts += 1
                    _emit("检测到密码输入框，正在输入密码并提交…")
                    page = getattr(driver, "page", None)
                    pwd_submitted = False
                    if page is not None:
                        try:
                            pwd_locator = page.locator('input[type="password"], input[name*="password" i], input[autocomplete="current-password"]').first
                            if pwd_locator.is_visible():
                                pwd_locator.click()
                                pwd_locator.fill("")  # 彻底清除已有内容，防止拼接累加
                                time.sleep(0.2)
                                page.keyboard.type(password)
                                time.sleep(0.3)
                                page.keyboard.press("Enter")
                                pwd_submitted = True
                                try:
                                    s_btn = page.locator('button[type="submit"], button.btn-primary').first
                                    if s_btn.is_visible():
                                        s_btn.click(delay=80, force=True)
                                except Exception:
                                    pass
                        except Exception as pe:
                            logger.warning("[提链] Playwright 原生输入密码异常，回退 JS: %s", pe)

                    if not pwd_submitted:
                        driver.execute_script("""
                            const pwd = document.querySelector('input[type="password"], input[name="current-password"], input[name="password"]');
                            if (pwd) {
                                pwd.focus();
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                if (setter) setter.call(pwd, arguments[0]); else pwd.value = arguments[0];
                                pwd.dispatchEvent(new Event('input', {bubbles: true}));
                                pwd.dispatchEvent(new Event('change', {bubbles: true}));
                                const form = pwd.closest('form');
                                if (form && form.requestSubmit) form.requestSubmit();
                            }
                        """, password)

                    # 等待跳转反馈 (网络代理下需预留 15 秒观察窗口)
                    wait_pwd = time.time()
                    while time.time() - wait_pwd < 15.0:
                        time.sleep(1.0)
                        cur_now = str(getattr(driver, "current_url", "") or "")
                        if any(k in cur_now for k in ["mfa", "challenge", "email-verification", "authenticator"]) or not _find_visible_password_input_js(driver):
                            break
                        err_now = driver.execute_script("""
                            const t = (document.body ? document.body.innerText : '').toLowerCase();
                            return t.includes('incorrect email address or password') || 
                                   t.includes('パスワードが正しくありません') ||
                                   t.includes('wrong password');
                        """)
                        if err_now:
                            logger.warning("[提链] 密码提交后检测到明确密码错误提示")
                            break
                    continue

                # 方案 2：若无密码、密码已试过、或页面显示密码错误，自动无缝切换至邮箱一次性验证码 (OTP)
                _emit("密码验证未通过或不可用，触发方案2：自动切换至一次性验证码 (OTP) 登录…")
                logger.info("[提链] 触发方案2：密码验证未通过或不可用，切换至 OTP/一次性验证码入口")
                clicked_otp = False
                page = getattr(driver, "page", None)
                if page is not None:
                    pwdless_selectors = [
                        "button[name='intent'][value='passwordless_login_send_otp']",
                        "input[type='submit'][name='intent'][value='passwordless_login_send_otp']",
                        "button[name='intent'][value='passwordless_signup_send_otp']",
                        "input[type='submit'][name='intent'][value='passwordless_signup_send_otp']",
                        "button[name='intent'][value*='passwordless'][value*='otp']",
                        "button[name='intent'][value*='passwordless'][value*='send_otp']",
                        "button:has-text('使用一次性验证码登录')",
                        "button:has-text('使用一次性验证码')",
                        "button:has-text('Use a one-time code')",
                        "button:has-text('Log in with a one-time code')",
                        "button:has-text('Continue with a one-time code')",
                        "button:has-text('ワンタイムコード')",
                        "button:has-text('認証コード')",
                        "button:has-text('メールでコード')",
                        "a:has-text('使用一次性验证码')",
                        "a:has-text('Use a one-time code')",
                        "a:has-text('ワンタイムコード')",
                        "a:has-text('メールでコード')",
                        "[role='button']:has-text('使用一次性验证码登录')",
                        "[role='button']:has-text('使用一次性验证码')",
                        "[role='button']:has-text('Continue with a one-time code')",
                        "[role='button']:has-text('one-time code')",
                        "button:has-text('他の方法')",
                        "button:has-text('別の方法')",
                        "button:has-text('Try another way')",
                        "button:has-text('Other options')",
                        "a:has-text('他の方法')",
                        "a:has-text('別の方法')",
                        "a:has-text('Try another way')",
                    ]
                    for sel in pwdless_selectors:
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible():
                                loc.click(force=True)
                                clicked_otp = True
                                _emit("已成功点击一次性验证码登录入口，等待验证码页面…")
                                break
                        except Exception:
                            continue

                if not clicked_otp:
                    try:
                        clicked_otp = bool(driver.execute_script(r"""
                            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                            const target = [...document.querySelectorAll('button, a, [role="button"]')].find(el => {
                                const val = (el.getAttribute('value') || '').toLowerCase();
                                const name = (el.getAttribute('name') || '').toLowerCase();
                                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                                if (val.includes('passwordless') || (name.includes('intent') && val.includes('otp'))) return true;
                                if (text.includes('一次性验证码') || text.includes('one-time code') || text.includes('ワンタイム') || text.includes('認証コード') || text.includes('メールでコード')) return true;
                                return false;
                            });
                            if (target && visible(target)) {
                                target.scrollIntoView({block:'center'});
                                target.click();
                                return true;
                            }
                            return false;
                        """))
                        if clicked_otp:
                            _emit("通过 DOM 点击了一次性验证码入口，等待页面过渡…")
                    except Exception as e:
                        logger.warning("[提链] DOM 点击一次性验证码异常: %s", e)

                # 方案 2 深度自愈：若密码页未提供直接 OTP 按钮，点击“忘记密码 (パスワードをお忘れですか？)”
                # 触发 OpenAI 邮箱自愈邮件，直接通过重置链接重新建立密码并自动登录
                if not clicked_otp:
                    forgot_clicked = False
                    if page is not None:
                        try:
                            forgot_loc = page.locator("a[href*='reset-password'], a:has-text('パスワードをお忘れですか'), a:has-text('Forgot password')").first
                            if forgot_loc.is_visible():
                                forgot_loc.click(force=True)
                                forgot_clicked = True
                        except Exception:
                            pass
                    if not forgot_clicked:
                        try:
                            forgot_clicked = bool(driver.execute_script(r"""
                                const a = [...document.querySelectorAll('a, button')].find(el => {
                                    const h = (el.getAttribute('href') || '').toLowerCase();
                                    const t = (el.textContent || '').trim().toLowerCase();
                                    return h.includes('reset-password') || t.includes('パスワードをお忘れ') || t.includes('forgot password');
                                });
                                if (a) { a.click(); return true; }
                                return false;
                            """))
                        except Exception:
                            pass

                    if forgot_clicked:
                        _emit("已点击【忘记密码】入口，正在请求邮箱自愈链接…")
                        time.sleep(2.5)
                        if page is not None:
                            try:
                                rst_btn = page.locator("form button[type='submit'], button[type='submit'], button.btn-primary, button:has-text('続行'), button:has-text('Continue')").first
                                if rst_btn.is_visible():
                                    rst_btn.click(force=True)
                            except Exception:
                                pass
                        driver.execute_script("const b = document.querySelector('button[type=\"submit\"], button.btn-primary'); if (b) b.click();")
                        _emit("已在重置页提交账号，等待接收重置邮件…")
                        time.sleep(3.0)
                        reset_code = None
                        ticket_url = None
                        poll_start = time.time()
                        from core.mailnest_client import _get_mails
                        import re
                        while time.time() - poll_start < 45.0:
                            try:
                                mails = _get_mails(email)
                                for m in mails:
                                    subj = str(m.get("subject") or "").lower()
                                    if "password" in subj or "パスワード" in subj or "code" in subj or "コード" in subj:
                                        body = str(m.get("content") or m.get("body") or "")
                                        # 1. 尝试提取 6 位数字验证码 (OpenAI 最新密码重置邮件格式)
                                        code_matches = re.findall(r"\b(\d{6})\b", body)
                                        if code_matches:
                                            reset_code = code_matches[0]
                                            break
                                        # 2. 尝试提取重置链接 (传统格式)
                                        matches = re.findall(r"https://auth\.openai\.com[^\s\"'<>]+", body)
                                        for link in matches:
                                            if "reset" in link or "password" in link:
                                                ticket_url = link.rstrip(".").rstrip(")")
                                                break
                                        if ticket_url:
                                            break
                                if reset_code or ticket_url:
                                    break
                            except Exception:
                                pass
                            time.sleep(2.5)

                        if reset_code:
                            _emit(f"已获取密码重置验证码 ({reset_code})，正在填写提交…")
                            if page is not None:
                                try:
                                    c_inp = page.locator('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"], input[inputmode="numeric"]').first
                                    if c_inp.is_visible():
                                        c_inp.click()
                                        c_inp.fill(str(reset_code))
                                        time.sleep(0.5)
                                        c_sub = page.locator('button:text-is("続行"), button:text-is("Continue"), button[type="submit"]').first
                                        if c_sub.is_visible():
                                            c_sub.click(force=True)
                                        else:
                                            page.keyboard.press("Enter")
                                except Exception as exc:
                                    logger.warning("[提链] 填写重置验证码异常: %s", exc)
                            time.sleep(3.0)

                        if ticket_url:
                            _emit("已收到重置自愈链接，正在完成密码设立与自动登录…")
                            driver.get(ticket_url)
                            time.sleep(3.5)

                        # 重置验证码或链接提交后，如出现新密码输入框，自动填写新密码并同步数据库
                        if page is not None:
                            try:
                                new_pwd = password or "%G6$C47ffq+KN8"
                                p_inp = page.locator("input#password-input, input[name*='password' i], input[type='password']").first
                                if p_inp.is_visible():
                                    p_inp.fill(new_pwd)
                                    time.sleep(0.5)
                                    p_sub = page.locator("button[type='submit'], button.btn-primary, button:text-is('続行'), button:text-is('Continue')").first
                                    p_sub.click(force=True)
                                    _emit("新密码已确认提交，正在进入 ChatGPT…")
                                    password = new_pwd
                                    try:
                                        from core import db
                                        db.update_account_password(account_id_db, new_pwd)
                                    except Exception:
                                        pass
                                    time.sleep(4.0)
                                    continue
                            except Exception as ex_rst:
                                logger.warning("[提链] 填写新密码异常: %s", ex_rst)

                otp_after_ts = time.time() - 2.0
                time.sleep(3.0)
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
                    page = getattr(driver, "page", None)
                    totp_filled = False
                    if page is not None:
                        try:
                            totp_loc = page.locator("input[name='code'], input[autocomplete='one-time-code'], input[inputmode='numeric'], input[type='text'], input[type='tel']").first
                            if totp_loc.is_visible():
                                totp_loc.click()
                                totp_loc.fill(code)
                                time.sleep(0.3)
                                page.keyboard.press("Enter")
                                totp_filled = True
                        except Exception:
                            pass
                    if not totp_filled:
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
                    time.sleep(3.0)
                except Exception as exc:
                    _emit(f"TOTP 提交尝试异常: {exc}")
                    time.sleep(2.0)
                continue

            # 7. 处于邮箱验证码 (OTP) 页面 (严格排除 MFA 页面及已跳转至主站页面的情况)
            is_otp_page = ("email-verification" in cur_url or "auth.openai.com/u/email-verification" in cur_url)
            if not is_otp_page and not is_mfa_page:
                try:
                    is_otp_page = bool(driver.execute_script("""
                        const inps = [...document.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"], input[inputmode="numeric"]')];
                        return inps.some(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    """))
                except Exception:
                    is_otp_page = False

            if is_otp_page and (time.time() - last_otp_submit_ts > 15.0):
                _emit("处于邮箱验证码页面，等待接收邮箱 OTP…")
                try:
                    otp_code = wait_for_otp(email, after_ts=otp_after_ts, max_wait=45, force_service=True)
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

                _emit(f"收到邮箱验证码 ({otp_code})，正在填写提交…")
                page = getattr(driver, "page", None)
                otp_filled = False
                if page is not None:
                    try:
                        otp_loc = page.locator('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"], input[inputmode="numeric"]').first
                        if otp_loc.is_visible():
                            otp_loc.click()
                            page.keyboard.type(str(otp_code))
                            otp_filled = True
                    except Exception:
                        pass
                if not otp_filled:
                    _clear_otp_inputs(driver)
                    _type_otp(driver, otp_code)
                time.sleep(1.0)
                try:
                    _click_continue(driver)
                except Exception:
                    pass
                try:
                    driver.execute_script("""
                        const btn = [...document.querySelectorAll('button')].find(b => {
                            const t = (b.innerText || '').toLowerCase();
                            return /continue|続行|继续|tiếp tục|next|submit/i.test(t) || b.type === 'submit';
                        });
                        if (btn && (btn.offsetWidth || btn.offsetHeight)) {
                            btn.click();
                        } else {
                            const form = document.querySelector('form');
                            if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
                        }
                    """)
                except Exception:
                    pass
                last_otp_submit_ts = time.time()
                otp_submitted = True
                time.sleep(3.0)
                continue

            time.sleep(1.5)

        if not access_token:
            _emit("正在读取 ChatGPT 真实登录会话凭证…")
            session_info = _fetch_chatgpt_session(driver, timeout=45, auto_jump_wait=30)
            access_token = session_info.get("accessToken")
            account_id = (session_info.get("account") or {}).get("id") or account_id

        if not access_token:
            try:
                from pathlib import Path
                screenshots_dir = Path("/app/注册日志/screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                shot_path = screenshots_dir / f"login_fail_{email}_{int(time.time())}.png"
                driver.save_screenshot(str(shot_path))
                cur = str(getattr(driver, "current_url", "") or "")
                logger.info("[提链] 登录未完成，现场快照已保存至 %s (url=%s)", shot_path, cur)
            except Exception:
                pass
            raise RuntimeError("指纹浏览器未能获取到有效 accessToken，登录未完成")

        chk_res = _do_checkout(access_token, account_id, "指纹环境已鉴权")
        if chk_res.get("ok"):
            return chk_res
        if chk_res.get("already_paid"):
            return chk_res

        # 提链未出链，保存现场截图并报错
        try:
            from pathlib import Path
            screenshots_dir = Path("/app/注册日志/screenshots")
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            shot_path = screenshots_dir / f"extract_fail_{email}_{int(time.time())}.png"
            driver.save_screenshot(str(shot_path))
            logger.info("[提链] 提链未出链，现场快照已保存至 %s", shot_path)
        except Exception:
            pass
        raise RuntimeError(f"指纹浏览器未能成功捕获结账链接: {str(chk_res)[:200]}")
    except Exception as exc:
        try:
            if driver:
                from pathlib import Path
                screenshots_dir = Path("/app/注册日志/screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                shot_path = screenshots_dir / f"extract_error_{email}_{int(time.time())}.png"
                driver.save_screenshot(str(shot_path))
                logger.info("[提链] 提链异常，现场快照已保存至 %s", shot_path)
        except Exception:
            pass
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _run_extract(*, account_id: int, trigger: str = "manual", link_type: str = "") -> dict:
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

    from config import extract_link as _extract_cfg
    target_lpm = str(link_type or acc.get("extract_link_type") or getattr(_extract_cfg, "EXTRACT_LINK_TYPE", "ideal") or "ideal").strip().lower()

    _append_log(account_id, f"提链任务启动：账号={email}，目标支付方式={target_lpm.upper()}，代理属地={country_badge}", clear=True)

    def _on_log(msg: str):
        _append_log(account_id, msg)
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "message": msg,
        })

    try:
        res = extract_checkout_url_with_cloak(
            account=acc_for_extract,
            proxy_url=matching_proxy,
            target_lpm=target_lpm,
            log_cb=_on_log,
        )
        if res.get("already_paid"):
            _append_log(account_id, "账号已是 Plus 会员")
            db.update_account_extract(account_id, {
                "ok": True,
                "status": "success",
                "message": "账号已是 Plus 会员",
            })
            logger.info("[提链] 账号 %s 已是 Plus 会员", email)
            return {"ok": True, "already_paid": True, "message": "账号已是 Plus 会员"}

        url = res.get("url")
        if not url:
            raise RuntimeError(f"未提取到有效支付链接: {res}")

        result_payload = {
            "short_url": res.get("short_url"),
            "long_url": res.get("long_url") or url,
            "lpm_url": res.get("lpm_url"),
            "copy_paste": res.get("copy_paste"),
            "image_url_png": res.get("qr_code"),
            "payment_method": res.get("type") or target_lpm,
            "payment_link_type": res.get("type") or target_lpm,
            "expires_at": res.get("expires_at"),
            "customer_session_client_secret": res.get("customer_session_client_secret"),
            "client_secret": res.get("client_secret"),
            "publishable_key": res.get("api_key"),
            "confirm_return_url": res.get("confirm_return_url"),
            "checkout_data": res.get("checkout_data"),
        }

        log_link_desc = f"提链成功：主链接={url}"
        if res.get("long_url") and res.get("long_url") != url:
            log_link_desc += f"\n免登长链={res.get('long_url')}"
        if res.get("short_url"):
            log_link_desc += f"\n站内短链={res.get('short_url')}"
        if res.get("lpm_url"):
            log_link_desc += f"\n直链跳转={res.get('lpm_url')}"
        _append_log(account_id, log_link_desc)

        db.update_account_extract(account_id, {
            "ok": True,
            "status": "success",
            "url": url,
            "link": url,
            "short_url": res.get("short_url"),
            "long_url": res.get("long_url") or url,
            "lpm_url": res.get("lpm_url"),
            "stripe_checkout_url": res.get("long_url") or url,
            "link_type": res.get("type") or target_lpm,
            "message": f"原生提链成功 ({res.get('name', target_lpm.upper())})",
            "result": result_payload,
        })
        logger.info("[提链] 账号 %s 提链成功 (%s): %s", email, target_lpm, url[:60])
        return {"ok": True, "url": url, "result": result_payload}
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {str(exc)}"
        _append_log(account_id, f"提链失败：{err_msg}")
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
    link_type: str = "",
    **kwargs: Any,
) -> dict:
    """将单账号提链任务加入执行队列。"""
    acc = db.get_account(account_id)
    if not acc:
        return {"accepted": False, "busy": False, "error": "账号不存在"}

    from config import extract_link as _extract_cfg
    chosen_type = str(link_type or acc.get("extract_link_type") or getattr(_extract_cfg, "EXTRACT_LINK_TYPE", "ideal") or "ideal").strip().lower()

    if not db.claim_account_extract(account_id, trigger=trigger, link_type=chosen_type):
        return {"accepted": False, "busy": True, "error": "该账号提链任务正在执行中"}

    fut = _EXTRACT_EXECUTOR.submit(_run_extract, account_id=account_id, trigger=trigger, link_type=chosen_type)
    return {"accepted": True, "busy": False, "future": fut}
