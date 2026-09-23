# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker
from core.cloakbrowser_driver import build_cloak_driver
from core.cloudflare_solver import solve_cloudflare_challenge_if_present
from core.email_provider import acquire_email_after_input, wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.proxy_dispatcher import (
    acquire_proxy_lease,
    is_proxy_connection_error,
    record_proxy_success,
    record_proxy_failure,
    ProxyLease,
)

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _is_signup_password_page,
)

logger = logging.getLogger(__name__)


def run_cloak_registration(
    email: str | None,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    traffic_tracker: PlaywrightTrafficTracker | None = None
    data_saver: BrowserDataSaver | None = None
    network_traffic: dict | None = None
    proxy_lease: ProxyLease | None = None
    attempted_proxies: set[str] = set()
    otp_after_ts = time.time()

    try:
        max_proxy_attempts = 3
        last_conn_exc = None
        for attempt in range(1, max_proxy_attempts + 1):
            _check_manual_stop()
            if proxy:
                effective_proxy = proxy
            else:
                proxy_lease = acquire_proxy_lease(exclude_urls=attempted_proxies)
                effective_proxy = proxy_lease.proxy_url if proxy_lease else None

            if effective_proxy:
                attempted_proxies.add(effective_proxy)

            try:
                driver, opened = build_cloak_driver(proxy=effective_proxy)
                try:
                    traffic_tracker = PlaywrightTrafficTracker(driver.context, label="Cloak")
                except Exception as exc:
                    # 统计失败不应影响注册主流程。
                    logger.warning("[Cloak注册] 初始化浏览器流量统计失败，继续注册：%s: %s", type(exc).__name__, str(exc)[:180])
                data_saver = BrowserDataSaver(label="Cloak")
                if traffic_tracker is not None:
                    traffic_tracker.attach_data_saver(data_saver)
                data_saver.install_playwright(driver.context)
                logger.info(
                    "[Cloak注册] 开始：%s，profile=%s，代理=%s（尝试 %d/%d）",
                    email, opened.profile_id, effective_proxy or "直连", attempt, max_proxy_attempts
                )

                otp_after_ts = time.time()
                logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
                driver.get("https://chatgpt.com/auth/login")

                # 页面成功加载，代理连通正常
                if proxy_lease and proxy_lease.proxy_id:
                    record_proxy_success(proxy_lease.proxy_id, effective_proxy)
                last_conn_exc = None
                break
            except Exception as exc:
                last_conn_exc = exc
                if is_proxy_connection_error(exc) and not proxy and attempt < max_proxy_attempts:
                    logger.warning(
                        "[Cloak注册] 代理连接失败 (%s)，记录失败并自动切换代理重试 (第 %d/%d 次): %s: %s",
                        effective_proxy, attempt, max_proxy_attempts, type(exc).__name__, str(exc)[:180]
                    )
                    if proxy_lease and proxy_lease.proxy_id:
                        record_proxy_failure(proxy_lease.proxy_id, effective_proxy, error=str(exc))
                    if driver:
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = None
                    if proxy_lease:
                        proxy_lease.release()
                        proxy_lease = None
                    continue
                else:
                    if proxy_lease and proxy_lease.proxy_id and is_proxy_connection_error(exc):
                        record_proxy_failure(proxy_lease.proxy_id, effective_proxy, error=str(exc))
                    raise

        if last_conn_exc:
            raise last_conn_exc

        human_delay("navigate")
        _maybe_accept(driver)
        solve_cloudflare_challenge_if_present(driver, max_wait=45.0)
        _check_manual_stop()

        def _email_supplier_after_input() -> str:
            nonlocal email
            _check_manual_stop()
            email = acquire_email_after_input(email)
            if on_email_acquired:
                on_email_acquired(email)
            return email

        next_state = _submit_email_and_wait_next(
            driver,
            email,
            attempts=3,
            email_supplier=_email_supplier_after_input,
        )
        _check_manual_stop()

        # 如果邮箱提交后直接进入验证码页，也尝试点击“使用密码继续”进入密码创建页；
        # _fill_password_page_if_present 会在设置成功后返回本次 OpenAI 注册密码。
        openai_password = _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        # 防御兜底：如果此时页面处于密码设置页且尚未设置密码，补充设密以进入 OTP 页
        if _is_signup_password_page(driver) and not openai_password:
            openai_password = _fill_password_page_if_present(driver, email, timeout=20)
            _check_manual_stop()

        # 进入 OTP 验证前，穿透可能存在的验证码页 Cloudflare 二次质询，触发验证码邮件下发
        solve_cloudflare_challenge_if_present(driver, max_wait=30.0)

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        if _twofa_cfg.ENABLE_2FA:
            logger.warning("[Cloak注册] 当前 CloakBrowser 自动化路径暂不执行 2FA 设置，已跳过")
        totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        # 统计注册浏览器关闭前的完整会话；注册后停留期间的网络请求也计入。
        post_register_dwell(email, label="Cloak注册")
        network_traffic = None
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception as exc:
                logger.warning("[Cloak注册] 停止流量统计异常（不影响账号落库）：%s: %s", type(exc).__name__, exc)
        if data_saver is not None:
            try:
                data_saver.stop()
            except Exception:
                pass
        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy_pool_target") if opened else None) or ((opened.raw or {}).get("proxy") if opened else None) or (proxy_lease.proxy_url if proxy_lease else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "network_traffic": network_traffic,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "network_traffic": network_traffic,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        if traffic_tracker is not None:
            try:
                network_traffic = traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            data_saver.stop()
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        if effective_proxy and ("cloudflare" in str(exc).lower() or "403" in str(exc)):
            try:
                from core.proxy_dispatcher import record_proxy_cooldown
                record_proxy_cooldown(effective_proxy, duration=180.0, reason="Cloudflare拦截/403")
            except Exception:
                pass
        try:
            if email:
                from core.email_provider import release_email
                release_email(email, status="failed" if create_acknowledged else "available", note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {
            "success": False,
            "email": email,
            "network_traffic": network_traffic,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if traffic_tracker is not None:
            try:
                traffic_tracker.stop()
            except Exception:
                pass
        if data_saver is not None:
            data_saver.stop()
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if proxy_lease:
            try:
                proxy_lease.release()
            except Exception:
                pass
