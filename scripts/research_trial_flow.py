# -*- coding: utf-8 -*-
"""
深入研究专项脚本：探究 0 元免单试用账号 (plus-1-month-free) 在 ChatGPT 中的原生弹窗、按钮文案与结账 Payload。
用于账号 950 (印度) 与 951 (越南) 的深度只读探测与分析。
"""
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from core import db
from core.cloakbrowser_driver import build_cloak_driver
from core.roxy_registration import (
    _find_visible_email_input_js,
    _type_email_address,
    _submit_nearest_form_for_active_input,
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
logger = logging.getLogger("research_trial_flow")


def research_account_trial_flow(account_id: int):
    acc = db.get_account(account_id)
    if not acc:
        logger.error("账号 %s 不存在", account_id)
        return

    email = acc["email"]
    country_code = acc.get("country_code") or "IN"
    totp_secret = acc.get("totp_secret")
    promo_id = acc.get("plus_trial_campaign_id") or "plus-1-month-free"

    proxy_url = db.pick_proxy_by_country(country_code, strict=False)
    logger.info("==================================================")
    logger.info("开始免单试用账号提链深度探究: ID=%s, 邮箱=%s, 属地=%s", account_id, email, country_code)
    logger.info("活动资格: %s, 代理: %s", promo_id, proxy_url)
    logger.info("==================================================")

    driver, _ = build_cloak_driver(proxy=proxy_url)
    try:
        driver.set_page_load_timeout(90)
        page = driver.page

        intercepted_requests = []
        intercepted_responses = []

        if hasattr(page, "route"):
            def handle_route(route, request):
                if "/backend-api/payments/checkout" in request.url and request.method == "POST":
                    try:
                        p_data = json.loads(request.post_data)
                        logger.info(">>> [CDP 拦截] 原始 checkout 请求体: %s", json.dumps(p_data, ensure_ascii=False, indent=2))
                        intercepted_requests.append(p_data)
                    except Exception as e:
                        logger.warning("解析 request.post_data 失败: %s", e)
                route.continue_()

            try:
                page.route("**/backend-api/payments/checkout", handle_route)
            except Exception as e:
                logger.warning("注册 route 失败: %s", e)

        def handle_response(response):
            if "/backend-api/payments/checkout" in response.url:
                try:
                    d = response.json()
                    logger.info("<<< [Network 响应] checkout 接口返回 (状态 %s): %s", response.status, json.dumps(d, ensure_ascii=False, indent=2))
                    intercepted_responses.append({"status": response.status, "data": d})
                except Exception as e:
                    logger.warning("解析 response 失败: %s", e)

        page.on("response", handle_response)

        # 1. 登录阶段
        logger.info("步骤 1: 访问 ChatGPT 登录页以建立会话…")
        driver.get("https://chatgpt.com/auth/login")
        time.sleep(3.0)

        t_end = time.time() + 180
        email_submitted = False
        otp_after_ts = time.time() - 2.0
        last_otp_submit_ts = 0.0
        totp_attempts = 0

        while time.time() < t_end:
            cur_url = str(driver.current_url or "")
            solve_cloudflare_challenge_if_present(driver, max_wait=10.0, emit_fn=logger.info)

            sess = None
            try:
                sess = _read_chatgpt_session_once(driver)
            except Exception:
                pass
            if sess and sess.get("accessToken"):
                logger.info("🎉 成功获取到登录会话 accessToken！")
                break

            if not email_submitted:
                el = _find_visible_email_input_js(driver)
                if el:
                    logger.info("输入邮箱: %s", email)
                    _type_email_address(driver, email, timeout=10)
                    time.sleep(1.0)
                    if _submit_nearest_form_for_active_input(driver):
                        email_submitted = True
                        otp_after_ts = time.time() - 2.0
                        logger.info("邮箱已提交，等待验证码…")
                        time.sleep(3.0)
                        continue

            if email_submitted and ("email-verification" in cur_url or "otp" in cur_url):
                if time.time() - last_otp_submit_ts > 10.0:
                    logger.info("正在接收邮箱 OTP…")
                    try:
                        code = wait_for_otp(email, after_ts=otp_after_ts, max_wait=45, force_service=True)
                    except Exception as ex_otp:
                        logger.warning("wait_for_otp 异常: %s", ex_otp)
                        code = None
                    if code:
                        logger.info("收到 OTP: %s，正在填入…", code)
                        driver.execute_script("""
                            const inputs = [...document.querySelectorAll('input')].filter(i => (i.offsetWidth || i.offsetHeight));
                            const otpInputs = inputs.filter(i => i.maxLength === 1 || i.name === 'code' || i.autocomplete === 'one-time-code');
                            if (otpInputs.length === 6) {
                                const digits = arguments[0].split('');
                                otpInputs.forEach((inp, idx) => {
                                    inp.value = digits[idx] || '';
                                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                                });
                            } else if (otpInputs.length === 1) {
                                otpInputs[0].value = arguments[0];
                                otpInputs[0].dispatchEvent(new Event('input', { bubbles: true }));
                            }
                        """, code)
                        last_otp_submit_ts = time.time()
                        time.sleep(2.0)
                        btn = driver.execute_script("""
                            const btns = [...document.querySelectorAll('button')];
                            return btns.find(b => /continue|tiếp tục|次へ|送信|submit/i.test(b.innerText || ''));
                        """)
                        if btn:
                            try:
                                btn.click()
                            except Exception:
                                pass
                        time.sleep(3.0)

            # TOTP 挑战
            if "mfa" in cur_url or "totp" in cur_url or "challenge" in cur_url:
                has_totp_input = driver.execute_script("""
                    const inputs = [...document.querySelectorAll('input')].filter(i => (i.offsetWidth || i.offsetHeight));
                    return inputs.some(i => i.autocomplete === 'one-time-code' || i.name === 'code' || i.placeholder.includes('code') || i.maxLength === 6);
                """)
                if has_totp_input and totp_secret and totp_attempts < 6:
                    totp_attempts += 1
                    token = pyotp.TOTP(totp_secret).now()
                    logger.info("提交 TOTP 动态码 (第 %d 次): %s", totp_attempts, token)
                    driver.execute_script("""
                        const inputs = [...document.querySelectorAll('input')].filter(i => (i.offsetWidth || i.offsetHeight));
                        const target = inputs.find(i => i.autocomplete === 'one-time-code' || i.name === 'code' || i.maxLength === 6) || inputs[0];
                        if (target) {
                            target.value = arguments[0];
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                            target.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    """, token)
                    time.sleep(1.0)
                    driver.execute_script("""
                        const form = document.querySelector('form');
                        if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
                    """)
                    time.sleep(3.5)

            time.sleep(1.5)

        # 2. 探索页面交互：进入带有活动参数的 URL
        logger.info("步骤 2: 导航进入带活动参数的 URL (不带 #pricing)...")
        target_url = f"https://chatgpt.com/?promo_campaign={promo_id}"
        driver.get(target_url)
        time.sleep(5.0)
        solve_cloudflare_challenge_if_present(driver, max_wait=15.0, emit_fn=logger.info)

        # 检查并打印页面状态与按钮
        state1 = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || (el.getClientRects && el.getClientRects().length));
            const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal')].filter(visible);
            const allButtons = [...document.querySelectorAll('button, a[role="button"], div[role="button"]')].filter(visible).map(b => {
                const r = b.getBoundingClientRect();
                return {
                    tag: b.tagName,
                    text: (b.innerText || '').trim().slice(0, 50),
                    aria: b.getAttribute('aria-label') || '',
                    testid: b.getAttribute('data-testid') || '',
                    x: Math.round(r.left),
                    y: Math.round(r.top),
                    in_dialog: !!b.closest('[role="dialog"], [aria-modal="true"], .modal')
                };
            });
            return {
                url: window.location.href,
                has_dialog: dialogs.length > 0,
                dialog_titles: dialogs.map(d => (d.querySelector('h1, h2, h3, [role="heading"]') || {}).innerText || d.innerText.slice(0, 60)),
                buttons: allButtons
            };
        """)
        logger.info("=== [URL: 无 #pricing] 页面状态 ===")
        logger.info("当前 URL: %s", state1.get("url"))
        logger.info("是否存在弹窗: %s, 弹窗标题: %s", state1.get("has_dialog"), state1.get("dialog_titles"))
        logger.info("页面按钮列表 (前 20 个): %s", json.dumps(state1.get("buttons")[:20], ensure_ascii=False, indent=2))

        # 保存截图 1
        driver.save_screenshot("/tmp/research_no_hash.png")
        logger.info("已保存截图至 /tmp/research_no_hash.png")

        # 3. 再测试带 #pricing 的页面
        logger.info("步骤 3: 导航进入带 #pricing 的 URL...")
        target_url_hash = f"https://chatgpt.com/?promo_campaign={promo_id}#pricing"
        driver.get(target_url_hash)
        time.sleep(5.0)

        state2 = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || (el.getClientRects && el.getClientRects().length));
            const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal')].filter(visible);
            const allButtons = [...document.querySelectorAll('button, a[role="button"], div[role="button"]')].filter(visible).map(b => {
                const r = b.getBoundingClientRect();
                return {
                    tag: b.tagName,
                    text: (b.innerText || '').trim().slice(0, 50),
                    aria: b.getAttribute('aria-label') || '',
                    testid: b.getAttribute('data-testid') || '',
                    x: Math.round(r.left),
                    y: Math.round(r.top),
                    in_dialog: !!b.closest('[role="dialog"], [aria-modal="true"], .modal')
                };
            });
            return {
                url: window.location.href,
                has_dialog: dialogs.length > 0,
                dialog_titles: dialogs.map(d => (d.querySelector('h1, h2, h3, [role="heading"]') || {}).innerText || d.innerText.slice(0, 60)),
                buttons: allButtons
            };
        """)
        logger.info("=== [URL: 带 #pricing] 页面状态 ===")
        logger.info("当前 URL: %s", state2.get("url"))
        logger.info("是否存在弹窗: %s, 弹窗标题: %s", state2.get("has_dialog"), state2.get("dialog_titles"))
        logger.info("页面按钮列表 (前 20 个): %s", json.dumps(state2.get("buttons")[:20], ensure_ascii=False, indent=2))

        # 保存截图 2
        driver.save_screenshot("/tmp/research_with_hash.png")
        logger.info("已保存截图至 /tmp/research_with_hash.png")

        # 4. 在控制台发起不同形态的 checkout 原生请求探测
        logger.info("步骤 4: 执行不同入参的结账接口探测实验…")
        test_payloads = [
            # 实验 1: 原生特惠试用格式 (无 entry_point)
            {
                "name": "实验 1: 仅 promo_campaign",
                "body": {
                    "plan_name": "chatgptplusplan",
                    "checkout_ui_mode": "custom",
                    "promo_campaign": {
                        "promo_campaign_id": promo_id,
                        "is_coupon_from_query_param": True
                    }
                }
            },
            # 实验 2: 带 entry_point: special_offer_modal
            {
                "name": "实验 2: special_offer_modal + custom",
                "body": {
                    "plan_name": "chatgptplusplan",
                    "entry_point": "special_offer_modal",
                    "checkout_ui_mode": "custom",
                    "promo_campaign": {
                        "promo_campaign_id": promo_id,
                        "is_coupon_from_query_param": True
                    }
                }
            },
            # 实验 3: hosted 模式 + 目标国 IN + INR
            {
                "name": "实验 3: hosted + IN/INR + promo",
                "body": {
                    "plan_name": "chatgptplusplan",
                    "checkout_ui_mode": "hosted",
                    "billing_details": {
                        "country": "IN",
                        "currency": "INR"
                    },
                    "promo_campaign": {
                        "promo_campaign_id": promo_id,
                        "is_coupon_from_query_param": True
                    }
                }
            },
            # 实验 4: hosted 模式 + 不指定 billing (使用原属地)
            {
                "name": "实验 4: hosted + 默认属地 + promo",
                "body": {
                    "plan_name": "chatgptplusplan",
                    "checkout_ui_mode": "hosted",
                    "promo_campaign": {
                        "promo_campaign_id": promo_id,
                        "is_coupon_from_query_param": True
                    }
                }
            }
        ]

        for exp in test_payloads:
            logger.info("--------------------------------------------------")
            logger.info("正在执行: %s", exp["name"])
            logger.info("请求体: %s", json.dumps(exp["body"]))
            res = driver.execute_script("""
                const body = arguments[0];
                const done = arguments[arguments.length - 1];
                let headers = {
                    'Content-Type': 'application/json'
                };
                try {
                    const devId = localStorage.getItem('oai-device-id');
                    if (devId) headers['oai-device-id'] = devId;
                } catch(_) {}
                
                fetch('https://chatgpt.com/backend-api/payments/checkout', {
                    method: 'POST',
                    credentials: 'include',
                    headers: headers,
                    body: JSON.stringify(body)
                })
                .then(async r => {
                    let d = null;
                    try { d = await r.json(); } catch(e) { d = await r.text(); }
                    done({ status: r.status, ok: r.ok, data: d });
                })
                .catch(err => {
                    done({ ok: false, error: String(err) });
                });
            """, exp["body"])
            logger.info("执行结果 (状态 %s): %s", res.get("status"), json.dumps(res.get("data"), ensure_ascii=False, indent=2))
            time.sleep(2.0)

    finally:
        driver.quit()
        logger.info("探测完成，浏览器驱动已退出。")


if __name__ == "__main__":
    aid = int(sys.argv[1]) if len(sys.argv) > 1 else 950
    research_account_trial_flow(aid)
