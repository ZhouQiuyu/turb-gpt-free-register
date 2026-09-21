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
                    return /特別オファーを利用|オファーを利用|特典を利用|利用する|plus を試す|無料で試す|dùng thử ưu đãi đặc biệt|dùng thử plus|ưu đãi đặc biệt|claim special offer|try special offer|try plus|start trial|claim offer/i.test(t);
                }) || dialogBtns.find(b => {
                    // 弹窗内 ChatGPT Plus 卡片内部的按钮
                    const card = b.closest('div, section');
                    const cardText = card ? (card.innerText || '').toLowerCase() : '';
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン')) return false;
                    return (cardText.includes('chatgpt plus') || cardText.includes('plus')) &&
                           /利用|dùng thử|try|claim|start|get/i.test(t);
                }) || dialogBtns.find(b => {
                    // 弹窗内主要蓝色/高亮按钮 (非当前套餐和关闭)
                    const style = window.getComputedStyle(b);
                    const bg = style.backgroundColor || '';
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('lên go') || t.includes('lên pro') || t.includes('gói hiện tại') || t.includes('ご利用中のプラン')) return false;
                    const isBlue = bg.includes('37, 99, 235') || bg.includes('16, 163, 127') || (bg.includes('rgb(') && !bg.includes('255, 255, 255') && !bg.includes('0, 0, 0'));
                    return isBlue && /オファー|特典|plus|ưu đãi|trial|offer/i.test(t);
                });

                if (btn) {
                    btn.scrollIntoView({ block: 'center' });
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
                return /特別オファーを利用|オファーを利用|特典を利用|plus を試す|無料で試す|dùng thử ưu đãi đặc biệt|dùng thử plus|claim special offer|try special offer/i.test(t);
            });

            if (globalBtn) {
                globalBtn.scrollIntoView({ block: 'center' });
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
        例如侧边栏的「オファーを受け取る」/「Nhận ưu đãi」/「アップグレード」/「Upgrade」。
        """
        return driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            const allButtons = [...document.querySelectorAll('button, a, div[role="button"]')].filter(visible);

            // 1. 匹配侧边栏横幅/专属优惠入口 (中英日越)
            const entryBtn = allButtons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (t.includes('login') || t.includes('signin') || t.includes('lên go') || t.includes('lên pro')) return false;
                return (
                    t.includes('オファーを受け取る') ||
                    t.includes('特典を受け取る') ||
                    t.includes('nhận ưu đãi') ||
                    t.includes('claim offer') ||
                    t.includes('get offer') ||
                    t.includes('特別オファー') ||
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

    # 双保险：通过 Playwright locator 精确点击 + DOM click 派发确保事件触发
    if page:
        try:
            loc = page.locator('button:has-text("特別オファーを利用"), button:has-text("オファーを利用"), button:has-text("特典を利用"), button:has-text("Claim special offer"), [role="dialog"] button:has-text("利用"), [role="dialog"] button:has-text("試す")').first
            if loc.is_visible():
                loc.click(timeout=3000)
        except Exception:
            pass

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
        return {"ok": True, "url": stripe_url, "checkout_session_id": cs_id, "api_key": captured_pk}

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
            "api_key": captured_pk,
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
    entity = str(checkout_data.get("processor_entity") or ("openai_llc" if country == "US" else "openai_ie")).strip()
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

    def _convert_to_lpm_if_needed(res: dict) -> dict:
        if not res or not res.get("ok") or res.get("already_paid"):
            return res
        cs_id = str(res.get("checkout_session_id") or "").strip()
        raw_url = str(res.get("url") or "").strip()
        if not cs_id and "/c/pay/" in raw_url:
            cs_id = raw_url.split("/c/pay/")[-1].split("?")[0].split("#")[0]
        elif not cs_id and "/checkout/" in raw_url:
            cs_id = raw_url.rstrip("/").split("/")[-1]

        # 针对 oaics_* 原生站内结账会话
        if cs_id.startswith("oaics_") or "oaics_" in raw_url:
            entity = res.get("processor_entity") or ("openai_llc" if origin_country == "US" else "openai_ie")
            canonical_url = f"https://chatgpt.com/checkout/{entity}/{cs_id}"
            _emit("检测到 OpenAI 原生结账会话 (oaics_*)，已生成官方真实免登结账直链")
            logger.info("[提链] 识别到 oaics_* 原生结账会话: %s -> %s", cs_id, canonical_url)
            return {
                "ok": True,
                "url": canonical_url,
                "checkout_session_id": cs_id,
                "processor_entity": entity,
                "type": "chatgpt_checkout",
                "name": "ChatGPT 官方结账直链",
                "api_key": res.get("api_key"),
            }

        # 针对标准 Stripe cs_* 会话且要求三方支付 (LPM)
        lpm = str(target_lpm or "ideal").strip().lower()
        if lpm and lpm not in ("card", "direct", "none") and cs_id.startswith("cs_"):
            _emit(f"已捕获 Stripe 结账会话 ({cs_id[:16]}…)，正在调用 Stripe LPM 引擎提取【{lpm.upper()}】原生支付直链…")
            from core.stripe_lpm_engine import StripeLPMExtractor
            extractor = StripeLPMExtractor(
                session_url_or_id=cs_id,
                target_lpm=lpm,
                proxy=proxy_url,
                api_key=res.get("api_key"),
            )
            return extractor.run()
        return res

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

            # 校验浏览器当前是否已持有有效登录会话 (通过 /api/auth/session)
            session_data = None
            cur_check = str(getattr(driver, "current_url", "") or "")
            if not cur_check or "chatgpt.com" in cur_check or "mock" in cur_check.lower():
                try:
                    session_data = _read_chatgpt_session_once(driver)
                except Exception:
                    pass

            if session_data and session_data.get("accessToken"):
                _emit(f"存量会话有效，正在通过拟人化操作向 OpenAI 发起【{origin_country}】原生试用提链…")
                chk_res = _human_extract_checkout_url(driver, promo_campaign_id=promo_campaign_id, emit_fn=_emit, timeout=120.0, origin_country=origin_country)
                if chk_res.get("ok"):
                    return _convert_to_lpm_if_needed(chk_res)
                if chk_res.get("already_paid"):
                    return chk_res
                _emit("存量会话拟人化提链未直接出链，进入登录自愈流…")
            else:
                _emit("存量会话在当前浏览器环境中未就绪，切换至完整登录流程…")
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
                has_password_input = False
                try:
                    has_password_input = driver.execute_script("""
                        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
                          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                        const pwd = document.querySelector('input[type="password"], input[name="password"], input[autocomplete="current-password"]');
                        return !!(pwd && visible(pwd));
                    """)
                except Exception:
                    has_password_input = False
                if has_password_input:
                    if password:
                        password_attempts += 1
                        if password_attempts > max_password_attempts:
                            raise RuntimeError("OpenAI 密码验证失败次数过多，可能密码已被更改或账号受限")
                        _emit(f"检测到密码输入框，正在输入密码并提交 (第 {password_attempts}/{max_password_attempts} 次)…")
                        driver.execute_script("""
                            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
                              && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                            const pwd = [...document.querySelectorAll('input[type="password"], input[name="password"], input[autocomplete="current-password"]')]
                              .find(visible);
                            if (!pwd) return false;
                            const val = arguments[0];
                            pwd.focus();
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(pwd, val); else pwd.value = val;
                            pwd.dispatchEvent(new Event('input', {bubbles: true}));
                            pwd.dispatchEvent(new Event('change', {bubbles: true}));

                            const form = pwd.closest('form');
                            const scope = form || document;
                            const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social/;
                            const buttons = [...scope.querySelectorAll('button, input[type="submit"]')]
                              .filter(el => visible(el) && !bad.test((el.className || '' + el.innerText).toLowerCase()))
                              .map((el, idx) => {
                                const r = el.getBoundingClientRect();
                                const ir = pwd.getBoundingClientRect();
                                return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom)};
                              })
                              .filter(x => x.below)
                              .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
                            if (buttons.length > 0) {
                              buttons[0].el.click();
                              return true;
                            }
                            if (form && typeof form.requestSubmit === 'function') {
                              form.requestSubmit();
                              return true;
                            }
                            pwd.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                            return true;
                        """, password)
                        try:
                            from selenium.webdriver.common.by import By
                            from selenium.webdriver.common.keys import Keys
                            pwd_els = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if e.is_displayed()]
                            if pwd_els:
                                pwd_els[0].send_keys(Keys.ENTER)
                        except Exception:
                            pass
                        time.sleep(3.5)
                        continue
                    else:
                        switch_pwdless_attempts += 1
                        if switch_pwdless_attempts > max_switch_pwdless_attempts:
                            raise RuntimeError("OpenAI 要求密码登录，但系统内未找到可用密码，且页面无一次性验证码入口")
                        _emit(f"检测到密码输入框但账号无可用密码，正在切换至一次性验证码登录 (尝试 {switch_pwdless_attempts}/{max_switch_pwdless_attempts})…")
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
            if not is_otp_page and not is_mfa_page:
                try:
                    is_otp_page = bool(driver.execute_script("""
                        const inps = [...document.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"], input[inputmode="numeric"]')];
                        return inps.some(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    """))
                except Exception:
                    is_otp_page = False

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
                # 强力兜底：通过 JS 主动点击包含 Tiếp tục/Continue/続行 的提交按钮或触发 form submit
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

        _emit(f"指纹环境已鉴权，正在通过拟人化操作向 OpenAI 发起【{origin_country}】原生提链…")
        chk_res = _human_extract_checkout_url(driver, promo_campaign_id=promo_campaign_id, emit_fn=_emit, timeout=120.0, origin_country=origin_country)
        if chk_res.get("ok"):
            return _convert_to_lpm_if_needed(chk_res)
        if chk_res.get("already_paid"):
            return chk_res

            # 拟人化提链未出链，保存现场截图并报错，坚决不退回协议请求
            try:
                from pathlib import Path
                screenshots_dir = Path("/app/注册日志/screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                shot_path = screenshots_dir / f"extract_fail_{email}_{int(time.time())}.png"
                driver.save_screenshot(str(shot_path))
                logger.info("[提链] 拟人化提链失败，现场快照已保存至 %s", shot_path)
            except Exception:
                pass
            raise RuntimeError(f"指纹浏览器拟人化提链未能成功捕获 Stripe 链接: {str(chk_res)[:200]}")
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
            "long_url": res.get("long_url") or url,
            "copy_paste": res.get("copy_paste"),
            "image_url_png": res.get("qr_code"),
            "payment_method": res.get("type") or target_lpm,
            "payment_link_type": res.get("type") or target_lpm,
            "expires_at": res.get("expires_at"),
        }

        _append_log(account_id, f"提链成功：{url}")
        db.update_account_extract(account_id, {
            "ok": True,
            "status": "success",
            "url": url,
            "link": url,
            "stripe_checkout_url": url,
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
