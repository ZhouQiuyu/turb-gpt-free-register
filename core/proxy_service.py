# -*- coding: utf-8 -*-
"""
代理池探活与质量检测服务。
支持：
1. 连通性测试 (Probe): 探测出口公网 IP、地理位置与 TCP 握手延迟。
2. OpenAI 质量检测 (Quality Check): 探测 api.openai.com，校验鉴权网关连通性与 Cloudflare 风控拦截状态。
3. 批量多线程并发测试。
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from core.db import get_proxy, proxy_to_url, update_proxy, list_proxies_page

logger = logging.getLogger(__name__)

# 探测端点配置
_PROBE_URLS = [
    {"url": "http://ip-api.com/json/?lang=zh-CN", "type": "ip-api"},
    {"url": "http://httpbin.org/ip", "type": "httpbin"},
]

_OPENAI_TARGET = "https://api.openai.com/v1/models"
_CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_cloudflare_challenge(status_code: int, headers: dict, body_text: str) -> bool:
    """检测响应是否命中 Cloudflare Challenge 5秒盾 / 人机验证。"""
    if status_code in (403, 503):
        if "cf-mitigated" in headers and headers["cf-mitigated"] == "challenge":
            return True
        lower_body = body_text.lower()
        if "just a moment" in lower_body or "cf-challenge" in lower_body or "turnstile" in lower_body:
            return True
    return False


def test_proxy_connectivity(proxy_id: int, timeout: float = 8.0) -> dict:
    """测试单个代理的连通性，获取实际出口 IP、地理位置与延迟。"""
    proxy = get_proxy(proxy_id)
    if not proxy:
        return {"success": False, "message": f"代理 #{proxy_id} 不存在"}

    proxy_url = proxy_to_url(proxy)
    last_err = ""

    # 优先尝试 curl_cffi，若不可用或报错则回退 requests
    for probe in _PROBE_URLS:
        url = probe["url"]
        start_time = time.time()
        try:
            resp = None
            try:
                from curl_cffi import requests as cffi_requests
                resp = cffi_requests.get(
                    url,
                    proxy=proxy_url,
                    timeout=timeout,
                    headers={"User-Agent": _CLIENT_USER_AGENT, "Accept": "application/json"},
                )
            except Exception:
                import requests
                proxies_dict = {"http": proxy_url, "https": proxy_url}
                resp = requests.get(
                    url,
                    proxies=proxies_dict,
                    timeout=timeout,
                    headers={"User-Agent": _CLIENT_USER_AGENT, "Accept": "application/json"},
                )

            latency_ms = int((time.time() - start_time) * 1000)
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                exit_ip = ""
                country = ""
                country_code = ""
                city = ""

                if probe["type"] == "ip-api":
                    if data.get("status") == "success":
                        exit_ip = str(data.get("query") or "").strip()
                        country = str(data.get("country") or "").strip()
                        country_code = str(data.get("countryCode") or "").strip()
                        city = str(data.get("city") or "").strip()
                    else:
                        continue
                elif probe["type"] == "httpbin":
                    exit_ip = str(data.get("origin") or "").split(",")[0].strip()

                updates = {
                    "latency_ms": latency_ms,
                    "latency_status": "success",
                    "latency_message": "连通正常",
                    "exit_ip": exit_ip,
                    "country": country,
                    "country_code": country_code,
                    "city": city,
                }
                update_proxy(proxy_id, updates)
                return {
                    "success": True,
                    "id": proxy_id,
                    "latency_ms": latency_ms,
                    "exit_ip": exit_ip,
                    "country": country,
                    "country_code": country_code,
                    "city": city,
                    "message": "连通正常",
                }
            else:
                last_err = f"HTTP {resp.status_code if resp else 'No response'}"
        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            last_err = str(e)

    # 全部探测失败
    err_msg = last_err or "探测超时或连接失败"
    if "timed out" in err_msg.lower():
        err_msg = "连接超时"
    elif "connection refused" in err_msg.lower():
        err_msg = "连接被拒绝"
    elif "407" in err_msg:
        err_msg = "407 代理认证失败"

    updates = {
        "latency_ms": None,
        "latency_status": "failed",
        "latency_message": err_msg,
    }
    update_proxy(proxy_id, updates)
    return {
        "success": False,
        "id": proxy_id,
        "latency_ms": None,
        "message": err_msg,
    }


def test_proxy_quality_openai(proxy_id: int, timeout: float = 10.0) -> dict:
    """针对 OpenAI 目标发起质量与风控检测。"""
    proxy = get_proxy(proxy_id)
    if not proxy:
        return {"success": False, "message": f"代理 #{proxy_id} 不存在"}

    proxy_url = proxy_to_url(proxy)
    now_iso = _now_iso()

    start_time = time.time()
    try:
        resp = None
        headers = {
            "User-Agent": _CLIENT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(
                _OPENAI_TARGET,
                proxy=proxy_url,
                timeout=timeout,
                headers=headers,
                impersonate="chrome120",
            )
        except Exception:
            import requests
            proxies_dict = {"http": proxy_url, "https": proxy_url}
            resp = requests.get(
                _OPENAI_TARGET,
                proxies=proxies_dict,
                timeout=timeout,
                headers=headers,
            )

        latency_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        resp_text = resp.text or ""

        # 1. 命中 Cloudflare Challenge
        if _is_cloudflare_challenge(status_code, resp_headers, resp_text):
            quality_status = "challenge"
            quality_score = 0
            quality_message = "命中 Cloudflare 盾/人机验证 (HTTP 403)"

        # 2. 白名单预期状态码 (401 Unauthorized 表明未带 Token 直达鉴权网关，未被拦截)
        elif status_code in (401, 200):
            quality_status = "pass"
            quality_score = 100
            quality_message = f"HTTP {status_code}（直达 OpenAI 鉴权网关，延迟 {latency_ms}ms）"

        # 3. 429 限流
        elif status_code == 429:
            quality_status = "warn"
            quality_score = 60
            quality_message = "HTTP 429（OpenAI 触发频控/限流）"

        # 4. 403 地区阻断或封锁
        elif status_code == 403:
            quality_status = "fail"
            quality_score = 0
            quality_message = "HTTP 403（IP 受限/地域不被 OpenAI 支持）"

        else:
            quality_status = "fail"
            quality_score = 0
            quality_message = f"非预期状态码 HTTP {status_code}"

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        quality_status = "fail"
        quality_score = 0
        err_str = str(e)
        if "timed out" in err_str.lower():
            quality_message = "OpenAI 请求超时"
        elif "refused" in err_str.lower():
            quality_message = "代理连接被拒绝"
        else:
            quality_message = f"请求失败: {err_str[:80]}"

    updates = {
        "quality_status": quality_status,
        "quality_score": quality_score,
        "quality_message": quality_message,
        "quality_checked_at": now_iso,
    }
    update_proxy(proxy_id, updates)

    return {
        "success": quality_status in ("pass", "warn"),
        "id": proxy_id,
        "quality_status": quality_status,
        "quality_score": quality_score,
        "quality_message": quality_message,
        "latency_ms": latency_ms,
    }


def batch_test_proxies(
    proxy_ids: list[int] | None = None,
    test_type: str = "connection",
    max_workers: int = 6,
) -> dict:
    """批量并发测试代理。
    
    注意：在连通性测试时，默认会对包含禁用状态在内的所有代理执行测试！
    """
    if not proxy_ids:
        # 用户未指定代理 ID 时，默认测试全部代理（包括已禁用的代理！）
        res = list_proxies_page(limit=500, offset=0)
        proxy_ids = [p["id"] for p in res.get("items", [])]

    if not proxy_ids:
        return {"total": 0, "tested": 0, "results": []}

    test_fn = test_proxy_connectivity if test_type == "connection" else test_proxy_quality_openai
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {executor.submit(test_fn, pid): pid for pid in proxy_ids}
        for future in as_completed(future_to_id):
            pid = future_to_id[future]
            try:
                data = future.result()
                results.append(data)
            except Exception as e:
                results.append({"id": pid, "success": False, "message": str(e)})

    success_count = sum(1 for r in results if r.get("success"))
    failed_count = len(results) - success_count
    pass_count = sum(1 for r in results if r.get("quality_status") == "pass")
    warn_count = sum(1 for r in results if r.get("quality_status") == "warn")
    fail_count = sum(1 for r in results if r.get("quality_status") in ("fail", "challenge"))

    return {
        "total": len(proxy_ids),
        "tested": len(results),
        "success": success_count,
        "failed": failed_count,
        "pass": pass_count,
        "warn": warn_count,
        "fail": fail_count,
        "results": results,
    }
