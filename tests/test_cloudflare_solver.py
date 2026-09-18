import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.cloudflare_solver import (
    is_cloudflare_challenge,
    solve_cloudflare_challenge_if_present,
    human_curve_move,
)


class TestCloudflareSolver(unittest.TestCase):
    def test_title_detection_multilang(self):
        # 英文
        driver = MagicMock(title="Just a moment...", current_url="https://chatgpt.com/auth/login", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

        # 泰文 (Job 20 实际遇到的场景)
        driver = MagicMock(title="รอสักครู่...", current_url="https://auth.openai.com/email-verification", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

        # 越南文 (Job 18 实际遇到的场景)
        driver = MagicMock(title="Chờ một chút...", current_url="https://chatgpt.com/auth/login", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

        # 日文
        driver = MagicMock(title="しばらくお待ちください...", current_url="https://chatgpt.com", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

        # 中文
        driver = MagicMock(title="请稍候...", current_url="https://chatgpt.com", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

        # 正常标题
        driver = MagicMock(title="ChatGPT", current_url="https://chatgpt.com", page=None)
        driver.execute_script.return_value = False
        self.assertFalse(is_cloudflare_challenge(driver))

    def test_url_detection(self):
        driver = MagicMock(title="OpenAI", current_url="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile", page=None)
        driver.execute_script.return_value = False
        self.assertTrue(is_cloudflare_challenge(driver))

    def test_playwright_frames_detection(self):
        driver = MagicMock(title="OpenAI", current_url="https://auth.openai.com/email-verification")
        driver.execute_script.return_value = False
        frame = MagicMock()
        frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/..."
        driver.page = MagicMock(frames=[frame])
        self.assertTrue(is_cloudflare_challenge(driver))

    def test_dom_script_detection(self):
        driver = MagicMock(title="OpenAI", current_url="https://chatgpt.com", page=None)
        driver.execute_script.return_value = True
        self.assertTrue(is_cloudflare_challenge(driver))

    def test_solve_when_no_challenge(self):
        driver = MagicMock(title="ChatGPT", current_url="https://chatgpt.com", page=None)
        driver.execute_script.return_value = False
        res = solve_cloudflare_challenge_if_present(driver, max_wait=2.0)
        self.assertFalse(res)

    def test_human_curve_move(self):
        mock_page = MagicMock()
        human_curve_move(mock_page, 100, 100, 300, 300, steps=5)
        self.assertTrue(mock_page.mouse.move.called)
        self.assertGreaterEqual(mock_page.mouse.move.call_count, 5)

    def test_solve_turnstile_container_coordinate_click(self):
        # 模拟 300x65 几何容器定位与拟真坐标点击
        mock_page = MagicMock()
        # evaluate 返回识别到的 300x65 widget rect
        mock_page.evaluate.return_value = {"x": 120.0, "y": 250.0, "w": 300.0, "h": 65.0}
        mock_page.frames = []

        driver = MagicMock()
        driver.page = mock_page
        driver.title = "Just a moment..."
        driver.current_url = "https://chatgpt.com/auth/login"
        driver.execute_script.return_value = False

        # 模拟点击后第二次检查时标题恢复正常
        def mock_title_effect():
            if mock_page.mouse.down.called or mock_page.mouse.click.called:
                return "ChatGPT"
            return "Just a moment..."

        type(driver).title = property(lambda self: mock_title_effect())

        emit_messages = []
        res = solve_cloudflare_challenge_if_present(driver, max_wait=5.0, emit_fn=emit_messages.append)
        self.assertTrue(res)
        self.assertTrue(mock_page.mouse.down.called or mock_page.mouse.click.called)
        self.assertTrue(any("拟真轨迹" in m or "质询" in m for m in emit_messages))

    def test_reject_footer_coordinate_click_falls_back_to_frames(self):
        # 模拟 evaluate 误命中页面底部页脚 (y=824 > 650)，必须被排除，并顺利回退到 Frame 穿透
        mock_box = MagicMock()
        mock_box.is_visible.return_value = True

        mock_frame = MagicMock()
        mock_frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"
        mock_frame.locator.return_value.count.return_value = 0
        mock_frame.locator.return_value.first = mock_box

        mock_page = MagicMock()
        # evaluate 返回了落在底部的页脚坐标
        mock_page.evaluate.return_value = {"x": 562.0, "y": 824.0, "w": 315.0, "h": 56.0, "source": "div"}
        mock_page.frames = [mock_frame]

        driver = MagicMock()
        driver.page = mock_page
        driver.title = "Just a moment..."
        driver.current_url = "https://chatgpt.com/auth/login"
        driver.execute_script.return_value = False

        def mock_title_effect():
            if mock_box.click.called:
                mock_page.frames = []
                return "ChatGPT"
            return "Just a moment..."

        type(driver).title = property(lambda self: mock_title_effect())

        emit_messages = []
        res = solve_cloudflare_challenge_if_present(driver, max_wait=5.0, emit_fn=emit_messages.append)
        self.assertTrue(res)
        # 证实：坐标点击绝没有在页脚执行 mouse.down
        self.assertFalse(mock_page.mouse.down.called)
        # 证实：顺畅回退到了真实的 Turnstile Frame 执行点击
        self.assertTrue(mock_box.click.called)

    def test_solve_click_turnstile_box(self):
        # 初始处于质询状态
        mock_box = MagicMock()
        mock_box.is_visible.return_value = True

        mock_frame = MagicMock()
        mock_frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"
        # 第一次 count 为 0（未 checked），后续为 1
        mock_frame.locator.return_value.count.return_value = 0
        mock_frame.locator.return_value.first = mock_box

        mock_page = MagicMock()
        # evaluate 返回非字典，触发向下层级 Frame 搜索
        mock_page.evaluate.return_value = None
        mock_page.frames = [mock_frame]

        driver = MagicMock()
        driver.page = mock_page
        driver.title = "รอสักครู่..."
        driver.current_url = "https://auth.openai.com/email-verification"
        driver.execute_script.return_value = False

        # 模拟点击后第二次检查时标题恢复正常且 frame 销毁（跳转放行）
        def mock_title_effect():
            if mock_box.click.called:
                mock_page.frames = []
                return "ChatGPT"
            return "รอสักครู่..."

        type(driver).title = property(lambda self: mock_title_effect())

        emit_messages = []
        res = solve_cloudflare_challenge_if_present(driver, max_wait=5.0, emit_fn=emit_messages.append)
        self.assertTrue(res)
        self.assertTrue(mock_box.click.called)
        self.assertTrue(any("质询" in m for m in emit_messages))

    def test_reject_auth_openai_header_coordinate_click_falls_back_to_frames(self):
        """验证在 auth.openai.com 页面上若误匹配顶部标题区域 (y <= 240)，被拒绝点击并平滑回退到真实 Frame 复选框。"""
        mock_frame = MagicMock()
        mock_frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/mndu3/0x4AAAAAAADnPIDROrmt1Wwj/light/normal"

        mock_box = MagicMock()
        mock_box.is_visible.return_value = True
        mock_box.count.return_value = 1
        mock_box_locator = MagicMock()
        mock_box_locator.first = mock_box
        mock_frame.locator.return_value = mock_box_locator

        mock_page = MagicMock()
        # 模拟 evaluate 误报了页面顶部标题区域坐标（如 y=150）
        mock_page.evaluate.return_value = {"x": 272, "y": 150, "w": 280, "h": 50, "source": "div"}
        mock_page.frames = [mock_frame]
        mock_page.url = "https://auth.openai.com/api/accounts/authorize?client_id=123"

        driver = MagicMock()
        driver.page = mock_page
        driver.title = "Just a moment..."
        driver.current_url = "https://auth.openai.com/api/accounts/authorize?client_id=123"
        driver.execute_script.return_value = False

        # 模拟点击后第二次检查时标题恢复正常且 frame 销毁（跳转放行）
        def mock_title_effect():
            if mock_box.click.called:
                mock_page.frames = []
                driver.current_url = "https://auth.openai.com/log-in"
                return "Sign in"
            return "Just a moment..."

        type(driver).title = property(lambda self: mock_title_effect())

        res = solve_cloudflare_challenge_if_present(driver, max_wait=5.0)
        self.assertTrue(res)
        # 顶部坐标应被安全守卫拦截（不触发鼠标点击），而 frame 复选框被成功点击
        self.assertFalse(mock_page.mouse.click.called)
        self.assertTrue(mock_box.click.called)

    def test_solve_turnstile_frame_element_coordinate_click(self):
        """测试通过 frame_element 的 bounding_box 穿透封闭 Shadow DOM 并成功模拟坐标点击。"""
        mock_frame_el = MagicMock()
        mock_frame_el.bounding_box.return_value = {"x": 273.0, "y": 304.0, "width": 298.0, "height": 65.0}

        mock_frame = MagicMock()
        mock_frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/mndu3/light/normal"
        mock_frame.frame_element.return_value = mock_frame_el
        mock_frame.locator.return_value.count.return_value = 0
        mock_frame.locator.return_value.first.is_visible.return_value = False

        mock_page = MagicMock()
        mock_page.evaluate.return_value = None  # 模拟 evaluate 未能通过 light DOM 获取到组件
        mock_page.frames = [mock_frame]
        mock_page.url = "https://auth.openai.com/api/accounts/authorize"

        driver = MagicMock()
        driver.page = mock_page
        driver.title = "Chờ một chút..."
        driver.current_url = "https://auth.openai.com/api/accounts/authorize"
        driver.execute_script.return_value = False

        def mock_title_effect():
            if mock_page.mouse.click.called or mock_page.mouse.down.called:
                mock_page.frames = []
                driver.current_url = "https://auth.openai.com/sign-up/password"
                return "Enter your password"
            return "Chờ một chút..."

        type(driver).title = property(lambda self: mock_title_effect())

        res = solve_cloudflare_challenge_if_present(driver, max_wait=5.0)
        self.assertTrue(res)
        # 证实：成功通过 frame_element 的坐标计算并触发了 mouse.click
        self.assertTrue(mock_page.mouse.click.called or mock_page.mouse.down.called)

    def test_detect_vietnamese_and_thai_titles(self):
        """测试越语与泰语 Cloudflare 质询标题能被快速准确识别。"""
        d1 = MagicMock(title="Chờ một chút...", current_url="https://chatgpt.com/auth/login", page=None)
        self.assertTrue(is_cloudflare_challenge(d1))

        d2 = MagicMock(title="รอสักครู่...", current_url="https://chatgpt.com/auth/login", page=None)
        self.assertTrue(is_cloudflare_challenge(d2))

        d3 = MagicMock(title="Thực hiện xác minh bảo mật", current_url="https://auth.openai.com/api/accounts/authorize", page=None)
        self.assertTrue(is_cloudflare_challenge(d3))


if __name__ == "__main__":
    unittest.main()
