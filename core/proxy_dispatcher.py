# -*- coding: utf-8 -*-
"""
代理并发调度器与动态禁用管理模块。

核心职责：
1. 并发感知调度 (Concurrency-Aware Lease)：
   - 追踪每个代理当前被占用的任务数 (_active_leases)。
   - 动态优先分配当前并发数为 0 的代理（0 并发优先，同并发按 LRU 调度，杜绝硬编码）。
2. 错误感知与容错自动切换：
   - 识别 net::ERR_CONNECTION_CLOSED 等底层代理网络断开。
   - 支持排除指定 URL，换选下一个可用代理重试。
3. 动态禁用 (Dynamic Auto-Ban)：
   - 维护每个代理在注册阶段的连续连接失败次数。
   - 连接成功时重置计数为 0。
   - 当“动态禁用”开关开启且连续 5 次连接失败时，自动在数据库中禁用该代理（status='disabled'）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from config.proxy import PROXY_POOL, normalize_proxy_url
from core import db

logger = logging.getLogger(__name__)

# 全局可重入锁保护内存调度状态
_lock = threading.RLock()

# 记录当前每个代理 URL 的活动租约数（并发占用数）
_active_leases: dict[str, int] = {}

# 记录每个代理 URL 上次被分配的时间戳（用于相同并发数时的 LRU 调度）
_last_used_ts: dict[str, float] = {}

# 记录每个代理（以数据库 id 为键）连续连接失败的次数
_consecutive_failures: dict[int, int] = {}

# 动态禁用开关内存缓存（None 时从数据库加载）
_auto_ban_enabled: bool | None = None

# 底层网络断开错误子串列表（区分于业务层人机验证挑战）
_PROXY_CONNECTION_ERROR_SUBSTRINGS = (
    "err_connection_closed",
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_socks_connection_failed",
    "err_connection_reset",
    "err_connection_refused",
    "err_timed_out",
    "err_empty_response",
    "err_name_not_resolved",
    "err_internet_disconnected",
    "failed to discover exit ip through proxy",
    "proxyconnectionerror",
    "socks5 connection failed",
    "general socks server failure",
    "connection reset by peer",
    "connection closed prematurely",
    "proxyerror",
    "curl: (7)",
    "curl: (35)",
    "curl: (97)",
    "proxy closed the connection",
)


def is_proxy_connection_error(exc: Any) -> bool:
    """判断异常是否属于代理网络断开/握手拒绝类错误。"""
    if not exc:
        return False
    msg = str(exc).lower()
    return any(sub in msg for sub in _PROXY_CONNECTION_ERROR_SUBSTRINGS)


def is_auto_ban_enabled() -> bool:
    """获取动态禁用开关状态（优先内存缓存，未初始化时查 storage_meta）。"""
    global _auto_ban_enabled
    with _lock:
        if _auto_ban_enabled is None:
            val = db.get_meta("proxy_auto_ban_enabled", "false")
            _auto_ban_enabled = str(val or "").strip().lower() in ("true", "1", "yes")
        return bool(_auto_ban_enabled)


def set_auto_ban_enabled(enabled: bool) -> None:
    """设置动态禁用开关状态并持久化至 storage_meta。"""
    global _auto_ban_enabled
    with _lock:
        _auto_ban_enabled = bool(enabled)
        db.set_meta("proxy_auto_ban_enabled", "true" if _auto_ban_enabled else "false")
    logger.info("[ProxyDispatcher] 动态禁用开关已更新为: %s", _auto_ban_enabled)


class ProxyLease:
    """
    代理租约对象。
    在注册任务运行期间持有该租约，使调度器知晓其当前并发数；
    任务完成或异常退出时调用 release()（或通过上下文管理器）安全归还租约。
    """

    def __init__(self, proxy_record: dict | None, proxy_url: str):
        self.proxy_record = dict(proxy_record or {})
        self.proxy_url = str(proxy_url or "")
        self.proxy_id = self.proxy_record.get("id")
        self._released = False

    def release(self) -> None:
        """释放租约，使代理当前并发占用数减 1。"""
        if not self._released:
            self._released = True
            _release_lease(self.proxy_url)

    def __enter__(self) -> ProxyLease:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"<ProxyLease id={self.proxy_id} url={self.proxy_url}>"


def _release_lease(proxy_url: str) -> None:
    """内部释放租约计数。"""
    if not proxy_url:
        return
    norm = normalize_proxy_url(proxy_url)
    with _lock:
        cur = _active_leases.get(norm, 0)
        if cur <= 1:
            _active_leases.pop(norm, None)
        else:
            _active_leases[norm] = cur - 1


def acquire_proxy_lease(
    exclude_urls: set[str] | list[str] | None = None,
    country_code: str | None = None,
) -> ProxyLease | None:
    """
    并发感知地申请一个代理租约。

    选择算法：
    1. 获取所有状态为 active 的可用代理；
    2. 过滤掉 exclude_urls 中的代理（例如本次任务已尝试过但连接失败的代理）；
    3. 动态排序 key = (当前并发数, 上次使用时间戳)：
       - 并发为 0 的空闲代理严格优先选择；
       - 当多个代理同为 0（或同为某并发数）时，按最久未使用的代理轮转；
    4. 成功申请后当前代理并发数 +1，返回 ProxyLease 对象。
    """
    exclude_set = {normalize_proxy_url(u) for u in (exclude_urls or []) if u}

    # 1. 尝试从数据库获取启用状态的代理
    candidates: list[dict] = []
    try:
        if country_code:
            candidates = db.get_active_proxies_by_country(country_code)
        else:
            candidates = db.get_active_proxies()
    except Exception as exc:
        logger.warning("[ProxyDispatcher] 从数据库读取活跃代理失败: %s", exc)

    # 2. 如果数据库没有启用代理，回退到 config.proxy.PROXY_POOL
    if not candidates:
        pool_urls = PROXY_POOL or []
        candidates = [{"id": None, "url": normalize_proxy_url(u)} for u in pool_urls if u]

    # 3. 过滤掉被排除的代理
    filtered = [
        c for c in candidates
        if normalize_proxy_url(c.get("url") or "") not in exclude_set
    ]

    if not filtered:
        logger.warning(
            "[ProxyDispatcher] 无候选代理可用 (总活跃候选=%d, 排除数=%d)",
            len(candidates), len(exclude_set)
        )
        return None

    with _lock:
        now = time.time()

        def _sort_key(item: dict) -> tuple[int, float]:
            norm = normalize_proxy_url(item.get("url") or "")
            concurrency = _active_leases.get(norm, 0)
            last_ts = _last_used_ts.get(norm, 0.0)
            return (concurrency, last_ts)

        chosen = min(filtered, key=_sort_key)
        norm_url = normalize_proxy_url(chosen.get("url") or "")
        _active_leases[norm_url] = _active_leases.get(norm_url, 0) + 1
        _last_used_ts[norm_url] = now

        logger.info(
            "[ProxyDispatcher] 分配代理: id=%s url=%s 当前并发=%d (候选数=%d)",
            chosen.get("id"), norm_url, _active_leases[norm_url], len(filtered)
        )
        return ProxyLease(proxy_record=chosen, proxy_url=norm_url)


def pick_best_proxy_url(
    country_code: str | None = None,
    exclude_urls: set[str] | list[str] | None = None,
) -> str:
    """
    非租约选优抽取（供兼容 pick_proxy 或轻量查询使用）。
    优先选取当前并发为 0 且最久未用的代理。
    """
    lease = acquire_proxy_lease(exclude_urls=exclude_urls, country_code=country_code)
    if not lease:
        return ""
    url = lease.proxy_url
    lease.release()
    return url


def record_proxy_success(proxy_id: int | None, proxy_url: str | None = None) -> None:
    """
    记录代理连接成功：将该代理的连续失败计数清零。
    """
    with _lock:
        if not proxy_id and proxy_url:
            p = db.find_proxy_by_url(proxy_url)
            if p:
                proxy_id = p.get("id")

        if proxy_id:
            old = _consecutive_failures.get(proxy_id, 0)
            if old > 0:
                logger.info("[ProxyDispatcher] 代理 ID=%s 页面连接成功，连续失败计数从 %d 清零", proxy_id, old)
            _consecutive_failures[proxy_id] = 0


def record_proxy_failure(
    proxy_id: int | None,
    proxy_url: str | None = None,
    error: str = "",
) -> dict:
    """
    记录代理连接失败：
    1. 连续失败次数 +1；
    2. 若开启动态禁用且连续失败 >= 5 次，自动将该代理置为 disabled。
    """
    with _lock:
        if not proxy_id and proxy_url:
            p = db.find_proxy_by_url(proxy_url)
            if p:
                proxy_id = p.get("id")

        if not proxy_id:
            return {"proxy_id": None, "consecutive_failures": 0, "banned": False}

        failures = _consecutive_failures.get(proxy_id, 0) + 1
        _consecutive_failures[proxy_id] = failures
        auto_ban = is_auto_ban_enabled()
        banned = False

        if auto_ban and failures >= 5:
            err_summary = str(error or "").strip()[:80]
            quality_msg = f"动态禁用：连续 {failures} 次连接失败 ({err_summary})"
            try:
                db.update_proxy(proxy_id, {
                    "status": "disabled",
                    "quality_message": quality_msg,
                    "latency_status": "failed",
                    "latency_message": f"连续 {failures} 次连接失败",
                })
                banned = True
                logger.warning(
                    "[ProxyDispatcher] 代理 ID=%s 连续 %d 次连接失败，已触发动态禁用并置为 disabled 状态！错误: %s",
                    proxy_id, failures, err_summary
                )
            except Exception as e:
                logger.exception("[ProxyDispatcher] 动态禁用代理 ID=%s 异常: %s", proxy_id, e)
        else:
            logger.warning(
                "[ProxyDispatcher] 代理 ID=%s 连接失败（连续失败: %d/5，动态禁用=%s）: %s",
                proxy_id, failures, auto_ban, str(error or "")[:120]
            )

        return {"proxy_id": proxy_id, "consecutive_failures": failures, "banned": banned}


def reset_proxy_failure(proxy_id: int) -> None:
    """重置代理的连续失败计数（例如在 WebUI 手动重新启用代理时）。"""
    with _lock:
        _consecutive_failures[proxy_id] = 0


def get_active_lease_count(proxy_url: str) -> int:
    """获取指定代理当前活跃租约数（调试/监控使用）。"""
    with _lock:
        return _active_leases.get(normalize_proxy_url(proxy_url), 0)


def get_consecutive_failures(proxy_id: int) -> int:
    """获取指定代理当前的连续失败次数。"""
    with _lock:
        return _consecutive_failures.get(proxy_id, 0)
