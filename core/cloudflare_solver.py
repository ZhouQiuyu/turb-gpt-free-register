# -*- coding: utf-8 -*-
"""
Cloudflare Turnstile 与人机安全质询检测与自动穿透模块。

支持多语言挑战标题（英、泰、越、日、中、西、法、德、俄、韩等）、
Turnstile IFrame 复选框识别与模拟点击、以及等待质询通过放行机制。
支持 CloakBrowser (Playwright Page) 与 RoxyBrowser (Selenium WebDriver)。
"""
from __future__ import annotations

import logging
import math
import random
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Cloudflare 质询页面常见标题关键词（跨语言）
_CF_TITLE_KEYWORDS = [
    # 英文
    "just a moment",
    "attention required",
    "security check",
    "checking your browser",
    "verifying you are human",
    "please wait",
    # 泰文
    "รอสักครู่",
    "กำลังทำการตรวจสอบ",
    "สักครู่",
    # 越南文
    "chờ một chút",
    "một chút",
    "thực hiện xác minh bảo mật",
    "xác minh bảo mật",
    "xác minh bạn là con người",
    "trang web này sử dụng dịch vụ bảo mật",
    # 日文
    "しばらくお待ちください",
    "あなたが人間であることを確認",
    # 中文
    "请稍候",
    "請稍候",
    "安全检查",
    "安全檢查",
    "正在检查",
    "正在檢查",
    "确认您是真人",
    # 西班牙文
    "un momento",
    # 法文
    "un instant",
    # 德文
    "einen moment",
    # 葡萄牙文
    "aguarde um momento",
    # 韩文
    "잠시만 기다려",
    # 俄文
    "подождите",
    # 阿拉伯文
    "لحظة من فضلك",
    # 土耳其文
    "bir saniye",
    # 通用
    "cloudflare",
    "turnstile",
    "ddos-guard",
]

# 质询页面特征文本（如 Ray ID + Cloudflare）
_CF_BODY_KEYWORDS = [
    "ray id",
    "performance & security by cloudflare",
    "กำลังทำการตรวจสอบความปลอดภัย",  # 泰语：正在进行安全检查
    "trang web này sử dụng dịch vụ bảo mật",  # 越南语：该网站使用安全服务
    "this website is using a security service to protect against online attacks",
    "verifying you are human",
    "确认您是真人",
    "あなたが人間であることを確認",
]


def _get_driver_prefix(driver: Any) -> str:
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak]"
    except Exception:
        pass
    return "[Cloudflare]"


def is_cloudflare_challenge(driver: Any) -> bool:
    """快速判断当前页面是否处于 Cloudflare 质询/验证状态。"""
    if driver is None:
        return False

    # 真实浏览器中 title 或 current_url 必为字符串；若皆非字符串则说明为未配置属性的 Mock 测试对象
    if not (isinstance(getattr(driver, "title", None), str) or isinstance(getattr(driver, "current_url", None), str)):
        return False

    # 1. 检查标题
    try:
        title = getattr(driver, "title", None)
        if isinstance(title, str) and title.strip():
            t = title.strip().lower()
            for kw in _CF_TITLE_KEYWORDS:
                if kw in t:
                    return True
    except Exception:
        pass

    # 2. 检查主页面 URL
    try:
        url = getattr(driver, "current_url", None)
        if isinstance(url, str) and url.strip():
            u = url.strip().lower()
            for pattern in ("challenges.cloudflare.com", "__cf_chl", "cf-challenge"):
                if pattern in u:
                    return True
    except Exception:
        pass

    # 3. 检查 Playwright Page 的 Frames (仅匹配 Turnstile 交互 iframe)
    page = getattr(driver, "page", None)
    if page is not None:
        try:
            frames = getattr(page, "frames", None)
            if callable(frames):
                frames = frames()
            if isinstance(frames, (list, tuple)):
                for f in frames:
                    f_url = getattr(f, "url", None)
                    if isinstance(f_url, str):
                        u = f_url.lower()
                        if "/turnstile/if/" in u or "challenges.cloudflare.com" in u:
                            return True
        except Exception:
            pass

    # 4. 检查 DOM 中是否存在质询特征容器或关键文本（同时排除正常可见输入框）
    try:
        if hasattr(driver, "execute_script") and callable(getattr(driver, "execute_script", None)):
            res = driver.execute_script(r"""
            try {
              const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
              const stage = document.querySelector('#challenge-stage, #cf-stage, .ctp-checkbox-label, iframe[src*="/turnstile/if/"], #challenge-error-text');
              if (stage && visible(stage)) return true;
              if (window._cf_chl_opt) return true;
              const input = document.querySelector('input[type="email"], input[name="email"], input[type="password"], textarea#prompt-textarea, [data-testid="login-button"]');
              if (input && visible(input)) return false;
              const text = (document.body ? document.body.innerText || '' : '').toLowerCase();
              if (text.includes('ray id') && text.includes('cloudflare')) return true;
              if (text.includes('กำลังทำการตรวจสอบความปลอดภัย')) return true;
              if (text.includes('trang web này sử dụng dịch vụ bảo mật')) return true;
              return false;
            } catch (_) {
              return false;
            }
            """)
            if res is True:
                return True
    except Exception:
        pass

    return False


def _save_cf_snapshot(driver: Any, label: str) -> None:
    """在 Cloudflare 质询关键节点抓取屏幕快照，供运维与调试审计。"""
    try:
        import os
        from datetime import datetime
        log_dir = "/app/注册日志/screenshots" if os.path.exists("/app") else "注册日志/screenshots"
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(log_dir, f"cf_{label}_{ts}.png")
        saved = False
        if hasattr(driver, "save_screenshot") and callable(driver.save_screenshot):
            saved = bool(driver.save_screenshot(filename))
        elif hasattr(driver, "page") and driver.page and hasattr(driver.page, "screenshot"):
            driver.page.screenshot(path=filename)
            saved = True
        if saved:
            logger.info("[Cloudflare] 已保存现场快照: %s", filename)
    except Exception as exc:
        logger.debug("[Cloudflare] 保存现场快照失败: %s", exc)


def human_curve_move(
    page: Any,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    steps: int = 12,
) -> None:
    """模拟人类带微颤的平滑缓动贝塞尔鼠标轨迹。"""
    try:
        mouse = getattr(page, "mouse", None)
        if mouse is None or not hasattr(mouse, "move"):
            return
        for i in range(1, steps + 1):
            t = i / steps
            ease = 0.5 - 0.5 * math.cos(t * math.pi)
            cur_x = start_x + (end_x - start_x) * ease + random.uniform(-1.0, 1.0)
            cur_y = start_y + (end_y - start_y) * ease + random.uniform(-1.0, 1.0)
            mouse.move(cur_x, cur_y)
            time.sleep(random.uniform(0.01, 0.025))
        mouse.move(end_x, end_y)
    except Exception:
        pass


def solve_cloudflare_challenge_if_present(
    driver: Any,
    max_wait: float = 45.0,
    emit_fn: Callable[[str], None] | None = None,
) -> bool:
    """
    若当前页面处于 Cloudflare 质询状态，尝试自动寻找 Turnstile 复选框穿透并等待放行。

    :param driver: CloakSeleniumDriver 或 Selenium WebDriver
    :param max_wait: 最大等待放行时间（秒，针对 Turnstile 渲染特点默认 45 秒）
    :param emit_fn: 进度回调输出函数（可选）
    :return: 若存在质询且成功穿透返回 True，若不存在质询返回 False，若质询超时未解返回 False。
    """
    if not is_cloudflare_challenge(driver):
        return False

    prefix = _get_driver_prefix(driver)
    title = str(getattr(driver, "title", "") or "")
    url = str(getattr(driver, "current_url", "") or "")
    logger.info("%s 检测到 Cloudflare 人机安全质询：title=%r url=%s", prefix, title, url[:120])
    _save_cf_snapshot(driver, "detected")
    if emit_fn:
        try:
            emit_fn("检测到 Cloudflare 人机安全质询，正在自动尝试穿透/等待放行…")
        except Exception:
            pass

    end = time.time() + max_wait
    clicked = False
    coord_click_count = 0
    last_coord_click_at = 0.0
    _CB_SELECTORS = (
        "input[type='checkbox']",
        ".ctp-checkbox-label",
        "#cf-stage",
        "span.mark",
        "[role='checkbox']",
        "div.ctp-checkbox-container",
        "#challenge-stage",
        "label.ctp-checkbox-label",
    )

    while time.time() < end:
        # 每轮优先检查是否已脱离质询页面（例如算力盾已自主放行或重定向）
        if not is_cloudflare_challenge(driver):
            logger.info("%s Cloudflare 人机安全质询已成功穿透放行！", prefix)
            _save_cf_snapshot(driver, "solved")
            if emit_fn:
                try:
                    emit_fn("Cloudflare 人机验证已成功通过！")
                except Exception:
                    pass
            return True

        now = time.time()
        page = getattr(driver, "page", None)
        round_clicked = False

        # 尝试通过 Playwright Page 穿透
        if page is not None:
            # 层级 0：自适应几何容器定位与拟真贝塞尔曲线周期性点击（带强制居中滚动保障）
            # Turnstile 容器通常为 300x65，先执行 scrollIntoView 居中，避免贴近底端（如 y=688）导致的点击盲区
            if (now - last_coord_click_at) >= 4.0:
                try:
                    widget = page.evaluate(r"""() => {
                        try {
                            const isExcluded = (el, r) => {
                                if (!r || r.width === 0 || r.height === 0) return true;
                                // 排除视口底部页脚/版权区域（例如 y > 650 或底部 80px 以内）以及顶部极端区域
                                if (r.y > 650 || r.bottom > (window.innerHeight - 80)) return true;
                                if (r.y < 40) return true;
                                if (el.closest && el.closest('footer, .footer, #footer, [role="contentinfo"], header, .header, #header')) return true;
                                // 严禁匹配标题或排版标签
                                const tag = (el.tagName || '').toUpperCase();
                                if (['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'B', 'STRONG'].includes(tag)) return true;
                                if (el.querySelector && el.querySelector('h1, h2, h3, h4, h5, h6')) return true;

                                // 如果是明确的 Turnstile 容器或 iframe，只要在有效视口区域内，绝不按文字排除
                                const isStageOrIframe = el.id === 'challenge-stage' || el.id === 'cf-stage' ||
                                    (el.className && typeof el.className === 'string' && el.className.includes('cf-turnstile')) ||
                                    (el.tagName === 'IFRAME' && ((el.getAttribute('src') || '').includes('challenge') || (el.getAttribute('src') || '').includes('turnstile')));
                                if (isStageOrIframe) {
                                    return false;
                                }

                                const text = (el.innerText || '').toLowerCase();
                                // 严禁匹配含有页面域名、安全验证说明大段文字的容器（如 <div>auth.openai.com</div>）
                                if (text.includes('auth.openai.com') || text.includes('chatgpt.com') || text.includes('openai.com') ||
                                    text.includes('thực hiện xác minh bảo mật') || text.includes('dịch vụ bảo mật để chống bot') ||
                                    text.includes('performing security') || text.includes('ray id')) return true;
                                return false;
                            };

                            const isValidWidgetSize = (r) => {
                                return r.width >= 240 && r.width <= 360 && r.height >= 40 && r.height <= 100;
                            };

                            // 1. 递归穿透所有 Shadow DOM 深度收集所有 iframe
                            const allIframes = [];
                            const queue = [document];
                            while (queue.length > 0) {
                                const node = queue.shift();
                                if (!node) continue;
                                if (node.querySelectorAll) {
                                    allIframes.push(...node.querySelectorAll('iframe'));
                                    const all = node.querySelectorAll('*');
                                    for (const el of all) {
                                        if (el.shadowRoot) queue.push(el.shadowRoot);
                                    }
                                }
                            }

                            // 优先检查含有 Cloudflare/Turnstile 特征 src 的 iframe
                            for (const ifr of allIframes) {
                                const src = (ifr.getAttribute('src') || '').toLowerCase();
                                const isTurnstileSrc = src.includes('challenges.cloudflare.com') || src.includes('challenge-platform') || src.includes('turnstile') || src.includes('cdn-cgi');
                                const r = ifr.getBoundingClientRect();
                                if (isTurnstileSrc && r.width > 0 && r.height > 0 && !isExcluded(ifr, r)) {
                                    ifr.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                                    const r2 = ifr.getBoundingClientRect();
                                    return { x: r2.x, y: r2.y, w: r2.width, h: r2.height, source: 'iframe' };
                                }
                            }

                            // 其次按标准 300x65 尺寸检查所有 iframe
                            for (const ifr of allIframes) {
                                const r = ifr.getBoundingClientRect();
                                if (isValidWidgetSize(r) && !isExcluded(ifr, r)) {
                                    ifr.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                                    const r2 = ifr.getBoundingClientRect();
                                    return { x: r2.x, y: r2.y, w: r2.width, h: r2.height, source: 'iframe-size' };
                                }
                            }

                            // 2. 检查含有 Turnstile 专属标识的容器（穿透 Shadow DOM）
                            const stageCandidates = [];
                            const sQueue = [document];
                            while (sQueue.length > 0) {
                                const node = sQueue.shift();
                                if (!node || !node.querySelectorAll) continue;
                                stageCandidates.push(...node.querySelectorAll('#challenge-stage, #cf-stage, .ctp-checkbox-container, [data-theme], [data-turnstile], [data-sitekey], .cf-turnstile, #cf-turnstile'));
                                const all = node.querySelectorAll('*');
                                for (const el of all) {
                                    if (el.shadowRoot) sQueue.push(el.shadowRoot);
                                }
                            }

                            for (const stage of stageCandidates) {
                                const r = stage.getBoundingClientRect();
                                if (isValidWidgetSize(r) && !isExcluded(stage, r)) {
                                    stage.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                                    const r2 = stage.getBoundingClientRect();
                                    return { x: r2.x, y: r2.y, w: r2.width, h: r2.height, source: 'stage' };
                                }
                                if (r.width > 360) {
                                    for (const child of stage.querySelectorAll('div, span, iframe')) {
                                        const cr = child.getBoundingClientRect();
                                        if (isValidWidgetSize(cr) && !isExcluded(child, cr)) {
                                            child.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                                            const cr2 = child.getBoundingClientRect();
                                            return { x: cr2.x, y: cr2.y, w: cr2.width, h: cr2.height, source: 'stage-child' };
                                        }
                                    }
                                    if (r.height >= 40 && r.height <= 120 && !isExcluded(stage, r)) {
                                        stage.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
                                        const r2 = stage.getBoundingClientRect();
                                        return { x: r2.x, y: r2.y, w: Math.min(r2.width, 300), h: Math.min(r2.height, 65), source: 'stage-box' };
                                    }
                                }
                            }
                        } catch (_) {}
                        return null;
                    }""")
                    curr_url = str(getattr(page, "url", "") or "").lower()
                    if isinstance(widget, dict) and widget.get("w", 0) >= 200 and widget.get("y", 9999) <= 650:
                        # 守卫：在 auth.openai.com 页面严防顶部标题误击（Turnstile 位于 y ≈ 304-370，绝不可能在 y <= 240）
                        if "auth.openai.com" in curr_url and widget.get("y", 0) <= 240:
                            logger.warning("%s [Cloudflare] 拒绝点击顶部标题区坐标 (y=%.1f <= 240 url=%s)，防范误击 domain 标题", prefix, widget.get("y", 0), curr_url[:80])
                        else:
                            cx = widget["x"] + 24.0 + random.uniform(-2.0, 2.0)
                            cy = widget["y"] + min(33.0, widget.get("h", 65.0) * 0.5) + random.uniform(-2.0, 2.0)
                            logger.info("%s [Cloudflare] 触发人类拟真轨迹坐标点击 Turnstile 容器: (%.1f, %.1f) [来源=%s]", prefix, cx, cy, widget.get("source", "unknown"))
                            if emit_fn and not clicked:
                                try:
                                    emit_fn("发现 Cloudflare Turnstile 验证框，正在模拟拟真轨迹点击…")
                                except Exception:
                                    pass
                            start_x = random.uniform(150, 350)
                            start_y = random.uniform(150, 350)
                            human_curve_move(page, start_x, start_y, cx, cy, steps=10)
                            time.sleep(random.uniform(0.12, 0.25))
                            mouse = getattr(page, "mouse", None)
                            if mouse is not None:
                                if hasattr(mouse, "down") and hasattr(mouse, "up"):
                                    mouse.down()
                                    time.sleep(random.uniform(0.08, 0.15))
                                    mouse.up()
                                elif hasattr(mouse, "click"):
                                    mouse.click(cx, cy)
                            last_coord_click_at = now
                            coord_click_count += 1
                            clicked = True
                            round_clicked = True
                            time.sleep(1.5)

                            # 协同 frame_locator 穿透：只要存在可见复选框即协同点击
                            for if_sel in (
                                "#challenge-stage iframe",
                                "#cf-stage iframe",
                                "iframe[src*='challenge-platform']",
                                "iframe[src*='challenges.cloudflare.com']",
                                "iframe[src*='turnstile']",
                                "iframe",
                            ):
                                try:
                                    fl = page.frame_locator(if_sel)
                                    for cb_sel in _CB_SELECTORS:
                                        box = fl.locator(cb_sel).first
                                        if box.is_visible():
                                            box.click(delay=random.randint(80, 150))
                                            logger.info("%s [Cloudflare] 协同 frame_locator(%s) 点击复选框", prefix, if_sel)
                                            break
                                except Exception:
                                    pass
                except Exception as exc:
                    logger.debug("%s [Cloudflare] 几何容器坐标点击异常: %s", prefix, exc)

            # 层级 1：遍历 Playwright Page 的 frames 寻找 Turnstile 框架与复选框
            try:
                frames = list(getattr(page, "frames", []) or [])
                for frame in frames:
                    f_url = str(getattr(frame, "url", "") or "").lower()
                    is_turnstile_frame = any(k in f_url for k in ("challenges.cloudflare.com", "challenge-platform", "turnstile", "cdn-cgi"))
                    if not is_turnstile_frame and frame == getattr(page, "main_frame", None):
                        continue

                    # 检查是否已勾选（正在提交或已放行）
                    try:
                        if frame.locator("input[type='checkbox']:checked, .ctp-checkbox-checked, #success").count() > 0:
                            time.sleep(1.0)
                            continue
                    except Exception:
                        pass

                    # 1. 优先通过 frame_element 的几何视口坐标穿透（绕过一切跨域与闭合 Shadow DOM 隔离）
                    if is_turnstile_frame and not round_clicked:
                        try:
                            frame_el = frame.frame_element()
                            fbox = frame_el.bounding_box()
                            if fbox and fbox.get("width", 0) >= 200 and 40 <= fbox.get("y", 0) <= 650:
                                fcx = fbox["x"] + 24.0 + random.uniform(-2.0, 2.0)
                                fcy = fbox["y"] + min(33.0, fbox.get("height", 65.0) * 0.5) + random.uniform(-2.0, 2.0)
                                logger.info("%s [Cloudflare] 发现 Turnstile Frame 几何坐标: (%.1f, %.1f) [frame=%s]，执行拟真轨迹点击", prefix, fcx, fcy, f_url[:60])
                                start_x = random.uniform(150, 350)
                                start_y = random.uniform(150, 350)
                                human_curve_move(page, start_x, start_y, fcx, fcy, steps=10)
                                mouse = getattr(page, "mouse", None)
                                if mouse is not None:
                                    if hasattr(mouse, "down") and hasattr(mouse, "up"):
                                        mouse.down()
                                        time.sleep(random.uniform(0.08, 0.15))
                                        mouse.up()
                                    elif hasattr(mouse, "click"):
                                        mouse.click(fcx, fcy)
                                last_coord_click_at = now
                                coord_click_count += 1
                                clicked = True
                                round_clicked = True
                                time.sleep(1.5)
                        except Exception as exc:
                            logger.debug("%s [Cloudflare] frame_element 几何坐标点击异常: %s", prefix, exc)

                    # 2. 检查 Frame 内部 DOM 元素选择器
                    for cb_sel in _CB_SELECTORS:
                        try:
                            box = frame.locator(cb_sel).first
                            if box.is_visible():
                                logger.info("%s [Cloudflare] 发现 Turnstile 复选框 (%s, frame=%s)，正在模拟点击…", prefix, cb_sel, f_url[:60])
                                if emit_fn and not clicked:
                                    try:
                                        emit_fn("发现 Cloudflare Turnstile 复选框，正在模拟点击…")
                                    except Exception:
                                        pass
                                try:
                                    box.scroll_into_view_if_needed(timeout=1500)
                                except Exception:
                                    pass
                                try:
                                    box.hover(timeout=1500)
                                    time.sleep(random.uniform(0.1, 0.25))
                                except Exception:
                                    pass
                                box.click(delay=random.randint(80, 160))
                                clicked = True
                                round_clicked = True
                                time.sleep(2.0)
                                break
                        except Exception:
                            pass

                    # 3. 兜底：对 Turnstile Frame body 执行相对位置 (24, 32) 穿透点击
                    if not round_clicked and is_turnstile_frame:
                        try:
                            body = frame.locator("body")
                            if body.is_visible():
                                body.click(position={"x": 24.0, "y": 32.0}, delay=random.randint(80, 150))
                                logger.info("%s [Cloudflare] 对 Turnstile Frame body 相对坐标 (24, 32) 执行穿透点击", prefix)
                                clicked = True
                                round_clicked = True
                                time.sleep(1.5)
                        except Exception:
                            pass

                    if round_clicked:
                        break
            except Exception as exc:
                logger.debug("%s [Cloudflare] Playwright Frame 遍历异常：%s", prefix, exc)

            # 层级 2：使用 frame_locator 强穿透
            if not round_clicked:
                for if_sel in (
                    "iframe[src*='challenge-platform']",
                    "iframe[src*='challenges.cloudflare.com']",
                    "iframe[src*='cdn-cgi']",
                    "iframe[title*='Cloudflare']",
                    "iframe[title*='challenge']",
                    "#cf-turnstile iframe",
                    "#turnstile-wrapper iframe",
                    "#challenge-stage iframe",
                ):
                    try:
                        fl = page.frame_locator(if_sel)
                        for cb_sel in _CB_SELECTORS:
                            box = fl.locator(cb_sel).first
                            if box.is_visible():
                                logger.info("%s [Cloudflare] 通过 frame_locator(%s) 发现复选框，执行点击…", prefix, if_sel)
                                box.click(delay=random.randint(80, 160))
                                clicked = True
                                round_clicked = True
                                time.sleep(2.0)
                                break
                        if not round_clicked:
                            try:
                                fl.locator("body").click(position={"x": 24.0, "y": 32.0}, delay=random.randint(80, 150))
                                logger.info("%s [Cloudflare] 通过 frame_locator(%s) body 坐标点击穿透", prefix, if_sel)
                                clicked = True
                                round_clicked = True
                                time.sleep(1.5)
                            except Exception:
                                pass
                        if round_clicked:
                            break
                    except Exception:
                        pass

            # 层级 3：模拟绝对屏幕坐标点击（穿透跨域/隔离 iframe）
            if not round_clicked:
                for if_sel in (
                    "#challenge-stage",
                    "#cf-stage",
                    ".cf-turnstile",
                    "iframe[src*='challenge-platform']",
                    "iframe[src*='challenges.cloudflare.com']",
                    "iframe[src*='cdn-cgi']",
                    "iframe[title*='Cloudflare']",
                    "iframe[title*='challenge']",
                    "#challenge-stage iframe",
                    "iframe",
                ):
                    try:
                        if_el = page.locator(if_sel).first
                        if if_el.is_visible():
                            bbox = if_el.bounding_box()
                            if bbox and bbox.get("width", 0) > 40 and bbox.get("height", 0) > 30 and 40 <= bbox.get("y", 9999) <= 650:
                                cx = bbox["x"] + min(25.0, bbox["width"] * 0.2)
                                cy = bbox["y"] + min(33.0, bbox["height"] * 0.5)
                                logger.info("%s [Cloudflare] 触发备用坐标点击穿透 Turnstile：%s x=%.1f y=%.1f", prefix, if_sel, cx, cy)
                                start_x = random.uniform(100, 300)
                                start_y = random.uniform(100, 300)
                                human_curve_move(page, start_x, start_y, cx, cy, steps=8)
                                time.sleep(random.uniform(0.1, 0.2))
                                mouse = getattr(page, "mouse", None)
                                if mouse is not None:
                                    if hasattr(mouse, "down") and hasattr(mouse, "up"):
                                        mouse.down()
                                        time.sleep(random.uniform(0.08, 0.15))
                                        mouse.up()
                                    elif hasattr(mouse, "click"):
                                        mouse.click(cx, cy, delay=random.randint(80, 150))
                                clicked = True
                                round_clicked = True
                                time.sleep(2.0)
                                break
                    except Exception:
                        pass

            # 层级 4：主框架备用兜底定位
            if not round_clicked:
                try:
                    main_box = page.locator("#challenge-stage input[type='checkbox'], #cf-stage, .ctp-checkbox-label").first
                    if main_box.is_visible():
                        logger.info("%s [Cloudflare] 发现主页面复选框，执行点击…", prefix)
                        main_box.click(delay=random.randint(80, 160))
                        clicked = True
                        round_clicked = True
                        time.sleep(2.0)
                except Exception:
                    pass

        # Selenium WebDriver 备用穿透
        elif hasattr(driver, "find_elements"):
            try:
                from selenium.webdriver.common.by import By
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    src = str(iframe.get_attribute("src") or "").lower()
                    if any(k in src for k in ("challenges.cloudflare.com", "challenge-platform", "turnstile", "cdn-cgi")):
                        driver.switch_to.frame(iframe)
                        for sel in ["input[type='checkbox']", ".ctp-checkbox-label", "#cf-stage", "[role='checkbox']"]:
                            boxes = driver.find_elements(By.CSS_SELECTOR, sel)
                            if boxes and boxes[0].is_displayed():
                                logger.info("%s [Cloudflare] Selenium 发现复选框并点击", prefix)
                                boxes[0].click()
                                clicked = True
                                time.sleep(2.0)
                                break
                        driver.switch_to.default_content()
                        if clicked:
                            break
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

        time.sleep(random.uniform(0.8, 1.2))

    # 结束后的最终判断
    if not is_cloudflare_challenge(driver):
        logger.info("%s Cloudflare 人机安全质询已通过！", prefix)
        _save_cf_snapshot(driver, "solved")
        return True

    _save_cf_snapshot(driver, "timeout")
    logger.warning("%s Cloudflare 质询等待超时 (%.1fs)，未能完成穿透", prefix, max_wait)
    return False
