import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.cloudflare_solver import is_cloudflare_challenge, solve_cloudflare_challenge_if_present


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


if __name__ == "__main__":
    unittest.main()
