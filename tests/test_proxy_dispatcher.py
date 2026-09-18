# -*- coding: utf-8 -*-
import pytest
from unittest.mock import patch, MagicMock
from core import proxy_dispatcher
from core.proxy_dispatcher import (
    acquire_proxy_lease,
    pick_best_proxy_url,
    record_proxy_success,
    record_proxy_failure,
    reset_proxy_failure,
    is_auto_ban_enabled,
    set_auto_ban_enabled,
    is_proxy_connection_error,
    get_consecutive_failures,
    get_active_lease_count,
    record_proxy_cooldown,
    is_proxy_cooling,
)


def test_is_proxy_connection_error():
    assert is_proxy_connection_error("Page.goto: net::ERR_CONNECTION_CLOSED at https://chatgpt.com/auth/login")
    assert is_proxy_connection_error("net::ERR_PROXY_CONNECTION_FAILED")
    assert is_proxy_connection_error("net::ERR_TUNNEL_CONNECTION_FAILED")
    assert is_proxy_connection_error("net::ERR_SOCKS_CONNECTION_FAILED")
    assert is_proxy_connection_error("Failed to discover exit IP through proxy")
    assert is_proxy_connection_error("ProxyConnectionError: SOCKS5 connection failed")
    
    # 业务层挑战或一般异常不应被误判为代理断开
    assert not is_proxy_connection_error("Cloudflare challenge detected")
    assert not is_proxy_connection_error("Invalid email or password")
    assert not is_proxy_connection_error(None)
    assert not is_proxy_connection_error("")


def test_concurrency_zero_priority():
    fake_proxies = [
        {"id": 1, "url": "socks5h://1.1.1.1:1080"},
        {"id": 2, "url": "socks5h://2.2.2.2:1080"},
        {"id": 3, "url": "socks5h://3.3.3.3:1080"},
    ]
    with patch("core.db.get_active_proxies", return_value=fake_proxies):
        # 清理内存状态
        with proxy_dispatcher._lock:
            proxy_dispatcher._active_leases.clear()
            proxy_dispatcher._last_used_ts.clear()

        # 第一次请求：应该租用其中一个（并发=0）
        lease1 = acquire_proxy_lease()
        assert lease1 is not None
        u1 = lease1.proxy_url
        assert get_active_lease_count(u1) == 1

        # 第二次请求：绝对不能重复分配已经并发为 1 的 u1，必须在另外两个并发为 0 的代理中选
        lease2 = acquire_proxy_lease()
        assert lease2 is not None
        u2 = lease2.proxy_url
        assert u2 != u1
        assert get_active_lease_count(u2) == 1

        # 第三次请求：必须分配给最后一个并发为 0 的代理
        lease3 = acquire_proxy_lease()
        assert lease3 is not None
        u3 = lease3.proxy_url
        assert u3 not in (u1, u2)
        assert get_active_lease_count(u3) == 1

        # 此时三个代理并发均为 1。如果释放 lease1：
        lease1.release()
        assert get_active_lease_count(u1) == 0
        assert get_active_lease_count(u2) == 1
        assert get_active_lease_count(u3) == 1

        # 下一次请求应该自动、优先分配回并发为 0 的 u1！
        lease4 = acquire_proxy_lease()
        assert lease4.proxy_url == u1
        assert get_active_lease_count(u1) == 1

        # 清理释放
        lease2.release()
        lease3.release()
        lease4.release()
        assert get_active_lease_count(u1) == 0
        assert get_active_lease_count(u2) == 0
        assert get_active_lease_count(u3) == 0


def test_exclude_urls():
    fake_proxies = [
        {"id": 1, "url": "socks5h://1.1.1.1:1080"},
        {"id": 2, "url": "socks5h://2.2.2.2:1080"},
    ]
    with patch("core.db.get_active_proxies", return_value=fake_proxies):
        with proxy_dispatcher._lock:
            proxy_dispatcher._active_leases.clear()
            proxy_dispatcher._last_used_ts.clear()

        # 排除 1.1.1.1，必须返回 2.2.2.2
        lease = acquire_proxy_lease(exclude_urls=["socks5h://1.1.1.1:1080"])
        assert lease is not None
        assert lease.proxy_url == "socks5h://2.2.2.2:1080"
        lease.release()

        # 如果排除全部，返回 None
        none_lease = acquire_proxy_lease(exclude_urls=["socks5h://1.1.1.1:1080", "socks5h://2.2.2.2:1080"])
        assert none_lease is None


def test_dynamic_auto_ban_toggle():
    with patch("core.db.set_meta") as mock_set_meta,          patch("core.db.get_meta", return_value="true"):
        set_auto_ban_enabled(True)
        assert is_auto_ban_enabled() is True
        mock_set_meta.assert_called_with("proxy_auto_ban_enabled", "true")

        set_auto_ban_enabled(False)
        assert is_auto_ban_enabled() is False
        mock_set_meta.assert_called_with("proxy_auto_ban_enabled", "false")


def test_auto_ban_disabled_does_not_disable_proxy():
    with patch("core.db.set_meta"), patch("core.db.update_proxy") as mock_update:
        set_auto_ban_enabled(False)
        reset_proxy_failure(99)

        for i in range(1, 10):
            res = record_proxy_failure(99, error="Connection closed")
            assert res["banned"] is False
            assert res["consecutive_failures"] == i

        # 开关关闭时，即使失败 9 次也不禁用
        mock_update.assert_not_called()


def test_auto_ban_enabled_disables_after_five_failures():
    with patch("core.db.set_meta"), patch("core.db.update_proxy") as mock_update:
        set_auto_ban_enabled(True)
        reset_proxy_failure(88)

        # 前 4 次失败不禁用
        for i in range(1, 5):
            res = record_proxy_failure(88, error="net::ERR_CONNECTION_CLOSED")
            assert res["banned"] is False
            assert res["consecutive_failures"] == i
            mock_update.assert_not_called()

        # 第 5 次失败触发自动禁用
        res5 = record_proxy_failure(88, error="net::ERR_CONNECTION_CLOSED")
        assert res5["banned"] is True
        assert res5["consecutive_failures"] == 5
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[0] == 88
        assert args[1]["status"] == "disabled"
        assert "动态禁用" in args[1]["quality_message"]


def test_success_resets_failure_count():
    with patch("core.db.set_meta"), patch("core.db.update_proxy") as mock_update:
        set_auto_ban_enabled(True)
        reset_proxy_failure(77)

        # 失败 4 次
        for _ in range(4):
            record_proxy_failure(77, error="fail")
        assert get_consecutive_failures(77) == 4

        # 成功 1 次 -> 计数归零
        record_proxy_success(77)
        assert get_consecutive_failures(77) == 0

        # 再次失败 2 次 -> 计数为 2，不会触发自动禁用
        record_proxy_failure(77, error="fail")
        record_proxy_failure(77, error="fail")
        assert get_consecutive_failures(77) == 2
        mock_update.assert_not_called()


def test_webui_auto_ban_api():
    from webui.app import create_app
    app = create_app(auth_code="test-auth")
    app.config["TESTING"] = True
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    with patch("core.db.set_meta"), patch("core.db.get_meta", return_value="false"):
        set_auto_ban_enabled(False)
        r_get = client.get("/api/proxies/auto-ban-setting")
        assert r_get.status_code == 200
        assert r_get.get_json()["enabled"] is False

    with patch("core.db.set_meta"):
        r_post = client.post("/api/proxies/auto-ban-setting", json={"enabled": True})
        assert r_post.status_code == 200
        assert r_post.get_json()["enabled"] is True
        assert is_auto_ban_enabled() is True


def test_proxy_403_cooldown():
    fake_proxies = [
        {"id": 1, "url": "socks5h://1.1.1.1:1080"},
        {"id": 2, "url": "socks5h://2.2.2.2:1080"},
    ]
    with patch("core.db.get_active_proxies", return_value=fake_proxies):
        with proxy_dispatcher._lock:
            proxy_dispatcher._active_leases.clear()
            proxy_dispatcher._last_used_ts.clear()
            proxy_dispatcher._proxy_cooldown_until.clear()

        # 初始两者都未冷却
        assert not is_proxy_cooling("socks5h://1.1.1.1:1080")
        assert not is_proxy_cooling("socks5h://2.2.2.2:1080")

        # 标记 1.1.1.1 冷却 60 秒
        record_proxy_cooldown("socks5h://1.1.1.1:1080", duration=60.0, reason="HTTP 403")
        assert is_proxy_cooling("socks5h://1.1.1.1:1080")

        # 此时申请代理，虽然两者并发都为 0，但必须优先分配未冷却的 2.2.2.2！
        lease = acquire_proxy_lease()
        assert lease is not None
        assert lease.proxy_url == "socks5h://2.2.2.2:1080"
        lease.release()

        # 如果两个代理都冷却了，也能分配（不卡死任务），但仍可正常释放
        record_proxy_cooldown("socks5h://2.2.2.2:1080", duration=60.0, reason="HTTP 403")
        lease2 = acquire_proxy_lease()
        assert lease2 is not None
        lease2.release()
