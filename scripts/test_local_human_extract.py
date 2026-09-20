# -*- coding: utf-8 -*-
"""本地端到端测试：使用 CloakBrowser 指纹浏览器拟人化操作提取 ChatGPT 试用结账链接。"""
import json
import logging
import sys
import time
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from config import cloakbrowser as _cfg
_cfg.CLOAK_EXTRA_ARGS = ['--ignore-certificate-errors', '--allow-insecure-localhost']

from core import db
from core.cloakbrowser_driver import build_cloak_driver
from core.roxy_registration import (
    _find_visible_email_input_js,
    _type_email_address,
    _submit_nearest_form_for_active_input,
    _clear_otp_inputs,
    _type_otp,
    _click_continue,
    _read_chatgpt_session_once,
    solve_cloudflare_challenge_if_present,
)
from core.email_provider import wait_for_otp
import pyotp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_human_extract")


def test_human_extract(account_id: int = 359, headless: bool = True):
    acc = db.get_account(account_id)
    if not acc:
        logger.error("账号 %s 不存在", account_id)
        return False

    email = acc["email"]
    totp_secret = acc.get("totp_secret")
    promo_campaign = acc.get("plus_trial_campaign_id") or "plus-1-month-free"
    # 使用本地 Loon HTTP 代理通道 (127.0.0.1:1234)
    proxy_url = "http://127.0.0.1:1234"

    logger.info("=== 开始本地拟人化提链实验 ===")
    logger.info("目标账号: %s (ID: %s)", email, account_id)
    logger.info("试用活动: %s", promo_campaign)
    logger.info("本地代理通道: %s", proxy_url)

    stripe_url = None
    checkout_response_data = None

    driver, _ = build_cloak_driver(proxy=proxy_url)
    try:
        driver.set_page_load_timeout(60)
        page = driver.page

        # -------------------------------------------------------------
        # 1. 注册网络监听器（核心双保险捕获）
        # -------------------------------------------------------------
        def handle_response(response):
            nonlocal stripe_url, checkout_response_data
            url = response.url
            if "/backend-api/payments/checkout" in url:
                try:
                    data = response.json()
                    logger.info("[Network] 拦截到 /payments/checkout 响应: status=%s, keys=%s", response.status, list(data.keys()) if isinstance(data, dict) else type(data))
                    checkout_response_data = data
                    if isinstance(data, dict):
                        target = data.get("url") or data.get("checkout_session_id")
                        if target:
                            if not target.startswith("http"):
                                target = f"https://checkout.stripe.com/c/pay/{target}"
                            stripe_url = target
                            logger.info(">>> [成功] 从网络接口截获 Stripe 链接: %s <<<", stripe_url)
                except Exception as exc:
                    logger.warning("[Network] 解析 checkout 响应失败: %s", exc)
            elif "checkout.stripe.com" in url:
                logger.info("[Network] 观察到 Stripe URL 跳转: %s", url)
                if not stripe_url:
                    stripe_url = url

        def handle_framenavigated(frame):
            nonlocal stripe_url
            url = frame.url
            if "checkout.stripe.com" in url:
                logger.info("[Frame] 页面已导航到 Stripe 结账台: %s", url)
                stripe_url = url

        page.on("response", handle_response)
        page.on("framenavigated", handle_framenavigated)

        # -------------------------------------------------------------
        # 2. 登录并建立完整用户会话环境
        # -------------------------------------------------------------
        logger.info("正在打开 ChatGPT 登录入口…")
        for attempt in range(3):
            try:
                driver.get("https://chatgpt.com/auth/login")
                break
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning("打开登录入口失败 (重试 %d/3): %s", attempt + 1, e)
                time.sleep(2.0)
        time.sleep(3.0)

        t_end = time.time() + 180
        logged_in = False
        email_submitted = False
        otp_after_ts = time.time() - 2.0
        last_otp_submit_ts = 0.0
        totp_attempts = 0
        last_logged_url = ""

        while time.time() < t_end:
            try:
                cur_url = str(driver.current_url or "")
                base_url = cur_url.split("?")[0] if cur_url else ""
                if "chrome-error:" in cur_url or "chromewebdata" in cur_url:
                    logger.warning("检测到处于 Chrome 错误页 (%s)，正在尝试刷新恢复…", cur_url)
                    time.sleep(2.0)
                    try:
                        driver.refresh()
                    except Exception:
                        driver.get("https://chatgpt.com/auth/login")
                    time.sleep(3.0)
                    continue

                # 穿透 Cloudflare
                if solve_cloudflare_challenge_if_present(driver, max_wait=10.0, emit_fn=logger.info):
                    time.sleep(1.0)
                    continue

                # 检查会话状态
                session_data = None
                try:
                    session_data = _read_chatgpt_session_once(driver)
                except Exception:
                    pass

                if session_data and session_data.get("accessToken"):
                    logger.info("已成功获取有效登录会话 accessToken！")
                    logged_in = True
                    break

                # 处理邮箱输入
                if not email_submitted:
                    el = _find_visible_email_input_js(driver)
                    if el:
                        logger.info("正在输入账号邮箱: %s", email)
                        _type_email_address(driver, email, timeout=10)
                        time.sleep(1.0)
                        if _submit_nearest_form_for_active_input(driver):
                            email_submitted = True
                            otp_after_ts = time.time() - 2.0
                            logger.info("邮箱已提交，准备接收验证码…")
                            time.sleep(3.0)
                            continue

                # 处于密码输入页面 -> 无密码时点击切换至一次性验证码登录
                if email_submitted:
                    has_password_input = driver.execute_script("""
                        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
                          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                        const pwd = document.querySelector('input[type="password"], input[name="password"], input[autocomplete="current-password"]');
                        return !!(pwd && visible(pwd));
                    """)
                    if has_password_input:
                        logger.info("检测到密码输入框，账号无密码，正在切换至一次性验证码登录…")
                        from core.roxy_registration import _click_passwordless_signup_if_present
                        _click_passwordless_signup_if_present(driver)
                        time.sleep(2.5)
                        continue

                # 处理邮箱验证码 (OTP)
                is_otp_page = ("email-verification" in cur_url or "auth.openai.com/u/email-verification" in cur_url)
                if not is_otp_page:
                    is_otp_page = bool(driver.execute_script("""
                        return !!document.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[data-testid="otp-input"]');
                    """))

                if is_otp_page and (time.time() - last_otp_submit_ts > 30.0):
                    logger.info("正在等待 MailNest 接收 OTP 验证码…")
                    otp_code = wait_for_otp(email, after_ts=otp_after_ts, max_wait=40, force_service=True)
                    logger.info("获取到 OTP: %s，正在自动填入…", otp_code)
                    _clear_otp_inputs(driver)
                    _type_otp(driver, otp_code)
                    time.sleep(1.0)
                    try:
                        _click_continue(driver)
                    except Exception:
                        pass
                    last_otp_submit_ts = time.time()
                    time.sleep(3.0)
                    continue

                # 处理 TOTP 2FA
                is_mfa = any(k in cur_url.lower() for k in ["mfa", "challenge", "authenticator"])
                if not is_mfa:
                    body_text = driver.execute_script("return (document.body ? document.body.innerText : '').slice(0, 500);") or ""
                    if any(w in body_text for w in ["認証アプリ", "authenticator", "ワンタイム", "security code", "two-factor"]):
                        is_mfa = True

                if is_mfa and totp_secret and totp_attempts < 3:
                    totp_attempts += 1
                    code = pyotp.TOTP(totp_secret).now()
                    logger.info("检测到 TOTP 2FA，正在生成动态口令并提交: %s", code)
                    driver.execute_script("""
                        const inp = document.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[type="text"], input[type="tel"]');
                        if (inp) {
                            inp.value = arguments[0];
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            const btn = document.querySelector("button[type='submit'], form button");
                            if (btn) btn.click();
                        }
                    """, code)
                    time.sleep(4.0)
                    continue

                time.sleep(2.0)
            except Exception as exc:
                if "destroyed" in str(exc) or "navigation" in str(exc):
                    time.sleep(1.0)
                    continue
                logger.warning("循环内瞬态异常: %s", exc)
                time.sleep(1.5)

        if not logged_in:
            raise RuntimeError("登录超时，未能建立有效登录会话")

        # -------------------------------------------------------------
        # 3. 拟人化交互：定位并触发 Plus 升级
        # -------------------------------------------------------------
        logger.info("登录完成，正在导航进入 ChatGPT 主界面…")
        driver.get("https://chatgpt.com/")
        time.sleep(4.0)
        solve_cloudflare_challenge_if_present(driver, max_wait=10.0, emit_fn=logger.info)

        # 检查并自动关闭新人弹窗 / Got it / 閉じる / Dismiss 遮罩
        logger.info("检查并关闭可能存在的欢迎/通知弹窗 (Got it / 了解 / 閉じる)…")
        dismissed = driver.execute_script("""
            const btns = [...document.querySelectorAll('button')];
            const gotIt = btns.find(b => /got it|了解|閉じる|dismiss|close/i.test(b.innerText || ''));
            if (gotIt && (gotIt.offsetWidth || gotIt.offsetHeight)) {
                gotIt.click();
                return { ok: true, text: gotIt.innerText.trim() };
            }
            return { ok: false };
        """)
        logger.info("关闭欢迎弹窗结果: %s", dismissed)
        time.sleep(2.0)

        # 保存当前主界面现场截图
        try:
            driver.save_screenshot("/tmp/chatgpt_home.png")
            logger.info("已保存主界面截图至 /tmp/chatgpt_home.png")
        except Exception:
            pass

        # 打印当前页面所有可见按钮和可交互元素
        page_elements = driver.execute_script("""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const items = [...document.querySelectorAll('button, a, div[role="button"]')].filter(visible);
            return items.map(el => ({
                tag: el.tagName,
                text: el.innerText.trim(),
                testid: el.getAttribute('data-testid') || '',
                aria: el.getAttribute('aria-label') || '',
                role: el.getAttribute('role') || ''
            })).filter(i => i.text || i.testid || i.aria);
        """)
        logger.info("主界面可见交互元素 (共 %d 个): %s", len(page_elements), json.dumps(page_elements[:20], ensure_ascii=False))

        logger.info("正在寻找并点击侧边栏 / 菜单「Claim offer / Upgrade / アップグレード」按钮…")
        # 模拟点击侧边栏升级或专属优惠按钮
        upgrade_clicked = driver.execute_script("""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
            // 常见选择器列表
            const selectors = [
                'button[aria-label*="Claim offer"]',
                'button[data-testid="upgrade-button"]',
                'button[data-testid="pricing-button"]',
                'button[data-testid="sidebar-upgrade-button"]',
                'a[href*="/pricing"]',
                'div[data-testid="accounts-profile-button"]',
                'div[data-testid="profile-button"]',
                'button[aria-label*="Profile"]',
                'button[aria-label*="Settings"]'
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && visible(el)) {
                    el.scrollIntoView({ block: 'center' });
                    el.click();
                    return { ok: true, selector: sel, text: el.innerText.trim() };
                }
            }

            // 按文本匹配按钮（涵盖 Claim offer / 特典 / 试用 / Upgrade）
            const allButtons = [...document.querySelectorAll('button, a, div[role="button"]')].filter(visible);
            const targetBtn = allButtons.find(b => {
                const t = (b.innerText || '').toLowerCase();
                return (
                    t.includes('claim offer') ||
                    t.includes('claim') ||
                    t.includes('offer') ||
                    t.includes('特典') ||
                    t.includes('オファー') ||
                    t.includes('upgrade') ||
                    t.includes('アップグレード') ||
                    t.includes('plus') ||
                    t.includes('プラン')
                ) && !t.includes('login') && !t.includes('signin');
            });
            if (targetBtn) {
                targetBtn.scrollIntoView({ block: 'center' });
                targetBtn.click();
                return { ok: true, text: targetBtn.innerText.trim() };
            }

            return { ok: false };
        """)
        logger.info("点击升级/优惠入口结果: %s", upgrade_clicked)
        time.sleep(3.0)

        if stripe_url:
            logger.info(">>> 点击入口按钮后已直接截获 Stripe 链接: %s <<<", stripe_url)

        # 检查是否已弹出 Pricing / Offer 弹窗（最多等 6 秒）
        t_wait_modal = time.time() + 6.0
        modal_el = None
        while time.time() < t_wait_modal and not stripe_url:
            has_dialog = driver.execute_script("""
                const d = document.querySelector('div[role="dialog"], [data-testid="pricing-modal"], [data-testid="all-plans-modal"], div[aria-modal="true"]');
                return !!(d && (d.offsetWidth || d.offsetHeight));
            """)
            if has_dialog:
                modal_el = True
                break
            time.sleep(1.0)

        if not modal_el and not stripe_url:
            logger.info("未直接出现弹窗，尝试在弹出的个人菜单中寻找「Upgrade / Claim offer」项…")
            menu_clicked = driver.execute_script("""
                const menuItems = [...document.querySelectorAll('[role="menuitem"], button, div')];
                const upgradeItem = menuItems.find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return (
                        t.includes('claim offer') ||
                        t.includes('claim') ||
                        t.includes('upgrade') ||
                        t.includes('アップグレード') ||
                        t.includes('plus') ||
                        t.includes('特典')
                    ) && !t.includes('login');
                });
                if (upgradeItem) {
                    upgradeItem.click();
                    return { ok: true, text: upgradeItem.innerText.trim() };
                }
                return { ok: false };
            """)
            logger.info("个人菜单中点击 Upgrade/Claim 结果: %s", menu_clicked)
            time.sleep(3.0)

        # 保存弹窗截图
        try:
            driver.save_screenshot("/tmp/chatgpt_modal.png")
            logger.info("已保存弹窗截图至 /tmp/chatgpt_modal.png")
        except Exception:
            pass

        # -------------------------------------------------------------
        # 4. 在定价/优惠弹窗中点击 Plus 卡片的升级/试用按钮
        # -------------------------------------------------------------
        if not stripe_url:
            logger.info("正在定价/优惠弹窗中寻找 Plus 试用/结账确认按钮…")
            checkout_btn_clicked = driver.execute_script("""
                const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight);
                const dialog = document.querySelector('div[role="dialog"], div[aria-modal="true"]') || document.body;
                const buttons = [...dialog.querySelectorAll('button')].filter(visible);

                // 优先匹配弹窗内包含明确试用 / Plus / Claim offer / Continue 动作的按钮
                const plusBtn = buttons.find(b => {
                    const t = (b.innerText || '').trim();
                    return /claim offer|upgrade to plus|plus を試す|無料で試す|plus にアップグレード|try for free|try plus|get plus|upgrade|continue|get offer|claim/i.test(t);
                });

                if (plusBtn) {
                    plusBtn.scrollIntoView({ block: 'center' });
                    plusBtn.click();
                    return { ok: true, text: plusBtn.innerText.trim() };
                }

                return { ok: false, all_buttons: buttons.map(b => b.innerText.trim()).filter(Boolean) };
            """)
            logger.info("Plus/优惠确认按钮点击结果: %s", checkout_btn_clicked)

        # -------------------------------------------------------------
        # 5. 等待捕获 Stripe Checkout 链接 (Sentinel PoW 计算需 40~50s)
        # -------------------------------------------------------------
        logger.info("正在等待 Stripe 结账链接生成 (含 Sentinel PoW 计算，最长等待 90 秒)…")
        wait_start = time.time()
        while time.time() - wait_start < 90.0:
            if stripe_url:
                break
            cur = str(driver.current_url or "")
            if "checkout.stripe.com" in cur:
                stripe_url = cur
                break
            time.sleep(1.0)

        # 兜底缓冲 3 秒，防止网络回调在最后一刻到达
        if not stripe_url:
            time.sleep(3.0)

        if stripe_url:
            logger.info("==========================================================")
            logger.info("🎉 [提链成功] 成功获取到官方 Stripe 试用结账链接！")
            logger.info("Stripe URL: %s", stripe_url)
            logger.info("==========================================================")
            return stripe_url
        else:
            logger.error("未能捕获到 Stripe 链接，当前 URL: %s, 结账接口响应: %s", driver.current_url, checkout_response_data)
            return None

    finally:
        driver.quit()


if __name__ == "__main__":
    aid = int(sys.argv[1]) if len(sys.argv) > 1 else 756
    test_human_extract(aid)
