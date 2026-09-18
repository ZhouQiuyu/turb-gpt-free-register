# -*- coding: utf-8 -*-
"""
Cloudflare Turnstile 与人机安全质询检测与自动穿透模块。

支持多语言挑战标题（英、泰、越、日、中、西、法、德、俄、韩等）、
Turnstile IFrame 复选框识别与模拟点击、以及等待质询通过放行机制。
支持 CloakBrowser (Playwright Page) 与 RoxyBrowser (Selenium WebDriver)。
"""
from __future__ import annotations

import logging
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
    # 泰文
    "รอสักครู่",
    # 越南文
    "chờ một chút",
    # 日文
    "しばらくお待ちください",
    # 中文
    "请稍候",
    "請稍候",
    "安全检查",
    "安全檢查",
    "正在检查",
    "正在檢查",
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

    # 2. 检查 URL
    try:
        url = getattr(driver, "current_url", None)
        if isinstance(url, str) and url.strip():
            u = url.strip().lower()
            for pattern in ("challenges.cloudflare.com", "challenge-platform", "__cf_chl", "cf-challenge", "/cdn-cgi/"):
                if pattern in u:
                    return True
    except Exception:
        pass

    # 3. 检查 Playwright Page 的 Frames
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
                        if any(p in u for p in ("challenges.cloudflare.com", "challenge-platform", "turnstile", "cdn-cgi")):
                            return True
        except Exception:
            pass

    # 4. 检查 DOM 中是否存在质询特征容器或关键文本
    try:
        if hasattr(driver, "execute_script") and callable(getattr(driver, "execute_script", None)):
            res = driver.execute_script(r"""
            try {
              const text = (document.body ? document.body.innerText || '' : '').toLowerCase();
              if (text.includes('ray id') && text.includes('cloudflare')) return true;
              if (text.includes('กำลังทำการตรวจสอบความปลอดภัย')) return true;
              if (text.includes('trang web này sử dụng dịch vụ bảo mật')) return true;
              if (document.querySelector('#challenge-stage, #cf-stage, .ctp-checkbox-label, iframe[src*="challenges.cloudflare.com"], iframe[src*="challenge-platform"], iframe[src*="cdn-cgi"]')) return true;
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


def solve_cloudflare_challenge_if_present(
    driver: Any,
    max_wait: float = 35.0,
    emit_fn: Callable[[str], None] | None = None,
) -> bool:
    """
    若当前页面处于 Cloudflare 质询状态，尝试自动寻找 Turnstile 复选框穿透并等待放行。

    :param driver: CloakSeleniumDriver 或 Selenium WebDriver
    :param max_wait: 最大等待放行时间（秒）
    :param emit_fn: 进度回调输出函数（可选）
    :return: 若存在质询且成功穿透返回 True，若不存在质询返回 False，若质询超时未解返回 False。
    """
    if not is_cloudflare_challenge(driver):
        return False

    prefix = _get_driver_prefix(driver)
    title = str(getattr(driver, "title", "") or "")
    url = str(getattr(driver, "current_url", "") or "")
    logger.info("%s 检测到 Cloudflare 人机安全质询：title=%r url=%s", prefix, title, url[:120])
    if emit_fn:
        try:
            emit_fn("检测到 Cloudflare 人机安全质询，正在自动尝试穿透/等待放行…")
        except Exception:
            pass

    end = time.time() + max_wait
    clicked = False
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
            if emit_fn:
                try:
                    emit_fn("Cloudflare 人机验证已成功通过！")
                except Exception:
                    pass
            return True

        # 尝试通过 Playwright Page 定位 frame 并点击
        page = getattr(driver, "page", None)
        if page is not None:
            try:
                frames = list(getattr(page, "frames", []) or [])
                for frame in frames:
                    f_url = str(getattr(frame, "url", "") or "").lower()
                    # 识别候选 Frame：非主框架，或包含 challenge / turnstile / cdn-cgi / about 等
                    is_candidate = (
                        frame != getattr(page, "main_frame", None)
                        or any(k in f_url for k in ("challenges.cloudflare.com", "challenge-platform", "turnstile", "cdn-cgi", "about:"))
                    )
                    if not is_candidate:
                        continue

                    # 检查是否已勾选（正在提交或已放行）
                    try:
                        if frame.locator("input[type='checkbox']:checked, .ctp-checkbox-checked, #success").count() > 0:
                            time.sleep(1.0)
                            continue
                    except Exception:
                        pass

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
                                time.sleep(2.0)
                                break
                        except Exception:
                            pass
                    if clicked:
                        break
            except Exception as exc:
                logger.debug("%s [Cloudflare] Playwright Frame 遍历异常：%s", prefix, exc)

            # 层级 2：使用 frame_locator 强穿透
            if not clicked:
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
                                time.sleep(2.0)
                                break
                        if clicked:
                            break
                    except Exception:
                        pass

            # 层级 3：模拟绝对屏幕坐标点击（穿透跨域/隔离 iframe）
            if not clicked:
                for if_sel in (
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
                            if bbox and bbox.get("width", 0) > 40 and bbox.get("height", 0) > 30:
                                # Turnstile 勾选框固定在 widget 左侧约 28px、垂直居中位置
                                cx = bbox["x"] + min(30.0, bbox["width"] * 0.15)
                                cy = bbox["y"] + (bbox["height"] / 2.0)
                                logger.info("%s [Cloudflare] 触发坐标点击穿透 Turnstile：x=%.1f y=%.1f", prefix, cx, cy)
                                page.mouse.move(cx, cy, steps=4)
                                time.sleep(random.uniform(0.1, 0.2))
                                page.mouse.click(cx, cy, delay=random.randint(80, 150))
                                clicked = True
                                time.sleep(2.5)
                                break
                    except Exception:
                        pass

            # 层级 4：主框架备用兜底定位
            if not clicked:
                try:
                    main_box = page.locator("#challenge-stage input[type='checkbox'], #cf-stage, .ctp-checkbox-label").first
                    if main_box.is_visible():
                        logger.info("%s [Cloudflare] 发现主页面复选框，执行点击…", prefix)
                        main_box.click(delay=random.randint(80, 160))
                        clicked = True
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
        return True

    logger.warning("%s Cloudflare 质询等待超时 (%.1fs)，未能完成穿透", prefix, max_wait)
    return False
