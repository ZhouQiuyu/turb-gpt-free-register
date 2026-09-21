# -*- coding: utf-8 -*-
"""
ChatGPT Plus 原生第三方支付提链协议引擎 (Stripe LPM Protocol Engine)。
严格依据 doc/chatgpt_link_extraction_technical_breakdown.md 规范实现。

支持的本地支付方式 (Local Payment Methods, LPMs):
- 🇳🇱 iDEAL (荷兰 - EUR)
- 🇧🇷 PIX (巴西 - BRL)
- 🇮🇳 UPI (印度 - INR)
- 🇰🇷 Kakao Pay (韩国 - KRW)
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)

DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"

# LPM 默认配置与国家参数映射 (严格对照文档第四节与第六节)
LPM_SPECS = {
    "ideal": {
        "country": "NL",
        "currency": "eur",
        "language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "default_bank": "rabobank",
        "requires_pre_confirm": False,
        "name": "iDEAL (荷兰)",
    },
    "pix": {
        "country": "BR",
        "currency": "brl",
        "language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "default_bank": "",
        "requires_pre_confirm": False,
        "name": "PIX (巴西)",
    },
    "upi": {
        "country": "IN",
        "currency": "inr",
        "language": "en-IN,en;q=0.9,en-US;q=0.8",
        "default_bank": "",
        "requires_pre_confirm": False,
        "name": "UPI (印度)",
    },
    "kakao_pay": {
        "country": "KR",
        "currency": "krw",
        "language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "default_bank": "",
        "requires_pre_confirm": True,
        "name": "Kakao Pay (韩国)",
    },
}


class StripeLPMExtractor:
    """
    Stripe LPM 协议提取引擎，遵循 12 步状态机：
    init -> update_tax -> (pre_confirm) -> confirm -> extract next_action
    """

    def __init__(
        self,
        session_url_or_id: str,
        target_lpm: str = "ideal",
        proxy: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        self.raw_input = str(session_url_or_id or "").strip()
        self.target_lpm = str(target_lpm or "ideal").strip().lower()
        if self.target_lpm not in LPM_SPECS:
            self.target_lpm = "ideal"
        self.spec = LPM_SPECS[self.target_lpm]

        self.proxy = proxy
        self.timeout = timeout

        # 解析 session_id 与 key
        parsed_id, parsed_key = self._parse_session_info(self.raw_input)
        self.session_id = parsed_id
        self.api_key = api_key or parsed_key or DEFAULT_STRIPE_PK

        # 状态跟踪指纹与会话 (文档第六节第 3 点)
        self.guid = str(uuid.uuid4())
        self.muid = str(uuid.uuid4())
        self.sid = str(uuid.uuid4())
        self.eid = None
        self.mrid = None

        # 运行时状态数据
        self.expected_amount = 2000  # 默认 20.00，后续由 update_tax 动态修正
        self.currency = self.spec["currency"]
        self.account_id = None
        self.payment_method_types: list[str] = []

        # 实例化 HTTP 会话
        self.session = self._build_session()

    def _parse_session_info(self, url_or_id: str) -> tuple[str, str | None]:
        """从 URL 或会话字串中提取 session_id (cs_live_...) 与 api_key (pk_live_...)。"""
        url_or_id = url_or_id.strip()
        if not url_or_id.startswith("http"):
            # 纯 session_id
            return url_or_id.split("#")[0], None

        u = urlparse(url_or_id)
        path_parts = [p for p in u.path.split("/") if p]
        cs_id = path_parts[-1] if path_parts else ""

        # 尝试从 fragment (#) 中解析
        key = None
        if u.fragment:
            frag = u.fragment
            if "apiKey=" in frag:
                q = parse_qs(frag)
                key = (q.get("apiKey") or [None])[0]
            elif frag.startswith("fidkd"):
                # Stripe 前端特定哈希参数
                pass

        if u.query:
            q = parse_qs(u.query)
            if not key and q.get("key"):
                key = q.get("key")[0]

        return cs_id, key

    def _build_session(self) -> Any:
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        if curl_requests is not None:
            s = curl_requests.Session(impersonate="chrome124")
            if proxies:
                s.proxies = proxies
            return s

        if requests is not None:
            s = requests.Session()
            if proxies:
                s.proxies = proxies
            return s

        return None

    def _common_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://checkout.stripe.com",
            "Referer": f"https://checkout.stripe.com/c/pay/{self.session_id}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": self.spec["language"],
        }

    def run(self) -> dict[str, Any]:
        """执行完整 12 步协议提链流程并返回结构化结果。"""
        if not self.session_id:
            raise ValueError("缺少有效的 Stripe Checkout Session ID")
        if self.session_id.startswith("oaics_"):
            raise ValueError(f"oaics_* 是 ChatGPT 原生内部结账会话，无法送入 Stripe payment_pages 接口")

        logger.info("[Stripe-LPM] 开始针对 Session %s 执行 %s 原生提链…", self.session_id[:16], self.spec["name"])

        # 步骤 1: 若缺少 api_key，尝试请求收银主页 HTML 提取上下文
        if not self.api_key:
            self.step_fetch_page_context()

        # 步骤 2: 初始化会话状态 (/init)
        self.step_init()

        # 步骤 3: 账单区域重算与增值税同步 (/update_taxes)
        self.step_update_tax()

        # 步骤 4: 预处理 (若目标支付方式需要 pre_confirm)
        if self.spec.get("requires_pre_confirm"):
            self.step_pre_confirm()

        # 步骤 5: 支付方式确认 (/confirm)
        confirm_data = self.step_confirm()

        # 步骤 6: 结果解构
        return self.parse_final_result(confirm_data)

    def step_fetch_page_context(self) -> None:
        """获取收银台 HTML 页面，提取 pk_live 公钥及加密配置。"""
        page_url = f"https://checkout.stripe.com/c/pay/{self.session_id}"
        headers = self._common_headers()
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        try:
            r = self.session.get(page_url, headers=headers, timeout=self.timeout)
            if r.status_code == 200:
                html = r.text
                pk_match = re.search(r'["\'](pk_live_[a-zA-Z0-9]+)["\']', html)
                if pk_match:
                    self.api_key = pk_match.group(1)
                    logger.info("[Stripe-LPM] 从页面上下文成功提取公钥: %s…", self.api_key[:12])
        except Exception as e:
            logger.debug("[Stripe-LPM] 请求页面上下文轻微警告: %s", e)

    def step_init(self) -> dict[str, Any]:
        """
        步骤 2: 请求 /v1/payment_pages/{cs_id}/init
        提取 payment_method_types、expected_amount 与商户标识。
        """
        url = f"https://api.stripe.com/v1/payment_pages/{self.session_id}/init"
        headers = self._common_headers()

        payload = {
            "browser_locale": self.spec["language"].split(",")[0],
            "browser_timezone": "Europe/Amsterdam" if self.spec["country"] == "NL" else "America/Sao_Paulo",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": self.spec["language"].split(",")[0],
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": self.api_key or DEFAULT_STRIPE_PK,
            "_stripe_version": STRIPE_VERSION_FULL,
        }

        r = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Stripe /init 接口响应异常 HTTP {r.status_code}: {r.text[:300]}")

        data = r.json()
        self.account_id = data.get("account_id")
        self.payment_method_types = data.get("payment_method_types") or []
        if data.get("eid"):
            self.eid = data.get("eid")
        init_amount = data.get("expected_amount") or (data.get("line_items", [{}])[0].get("amount_total"))
        if init_amount:
            try:
                self.expected_amount = int(init_amount)
            except Exception:
                pass

        if not self.api_key and data.get("api_key"):
            self.api_key = data.get("api_key")

        logger.info(
            "[Stripe-LPM] /init 完成: account=%s, methods=%s, 初始金额=%s",
            self.account_id,
            self.payment_method_types,
            self.expected_amount,
        )
        return data

    def step_update_tax(self) -> dict[str, Any]:
        """
        步骤 3: 请求 /v1/payment_pages/{cs_id}/update_taxes
        设置目标国家，触发 Stripe 重算本地税率并对齐 expected_amount (防 amount_mismatch)。
        """
        url = f"https://api.stripe.com/v1/payment_pages/{self.session_id}/update_taxes"
        headers = self._common_headers()

        payload = {
            "key": self.api_key or "",
            "eid": self.eid or "NA",
            "customer_shipping_address[country]": self.spec["country"],
            "customer_billing_address[country]": self.spec["country"],
            "currency": self.spec["currency"],
        }

        try:
            r = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                snapshot = data.get("snapshot") or {}
                amount_total = snapshot.get("amount_total") or data.get("expected_amount")
                if amount_total:
                    self.expected_amount = int(amount_total)
                    logger.info("[Stripe-LPM] 税费重算完成，已精准对齐 expected_amount=%s", self.expected_amount)
                return data
        except Exception as ex:
            logger.warning("[Stripe-LPM] update_taxes 异常，保持基础金额: %s", ex)
        return {}

    def step_pre_confirm(self) -> dict[str, Any]:
        """步骤 4 (可选): 针对 Kakao Pay 等需要预处理的 LPM 执行 pre_confirm。"""
        url = f"https://api.stripe.com/v1/payment_pages/{self.session_id}/pre_confirm"
        headers = self._common_headers()
        payload = {
            "key": self.api_key or "",
            "eid": self.eid or "NA",
            "type": self.target_lpm,
            "expected_amount": self.expected_amount,
        }
        r = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        logger.info("[Stripe-LPM] pre_confirm 响应: status=%s", r.status_code)
        return r.json() if r.status_code == 200 else {}

    def step_confirm(self) -> dict[str, Any]:
        """
        步骤 5: 请求 /v1/payment_pages/{cs_id}/confirm
        提交支付方式确认，截获 next_action 中的跳转直链或二维码数据。
        """
        url = f"https://api.stripe.com/v1/payment_pages/{self.session_id}/confirm"
        headers = self._common_headers()

        payload = {
            "key": self.api_key or "",
            "eid": self.eid or "NA",
            "expected_amount": self.expected_amount,
            "payment_method_data[type]": self.target_lpm,
            "payment_method_data[billing_details][address][country]": self.spec["country"],
            "guid": self.guid,
            "muid": self.muid,
            "sid": self.sid,
        }

        # 针对各支付方式补充特定字段
        if self.target_lpm == "ideal":
            bank = self.spec.get("default_bank") or "rabobank"
            payload["payment_method_data[ideal][bank]"] = bank
        elif self.target_lpm == "pix":
            payload["payment_method_data[billing_details][name]"] = "Subscriber"
        elif self.target_lpm == "upi":
            payload["payment_method_data[upi][vpa]"] = "customer@okaxis"

        r = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Stripe /confirm 支付确认失败 HTTP {r.status_code}: {r.text[:300]}")

        data = r.json()
        logger.info("[Stripe-LPM] /confirm 成功返回: keys=%s", list(data.keys()))
        return data

    def parse_final_result(self, data: dict[str, Any]) -> dict[str, Any]:
        """解析 confirm 响应中的 next_action，输出标准化的提链资产。"""
        payment_intent = data.get("payment_intent") or data
        next_action = payment_intent.get("next_action") or {}
        action_type = next_action.get("type") or ""

        out = {
            "ok": True,
            "type": self.target_lpm,
            "name": self.spec["name"],
            "session_id": self.session_id,
            "url": None,
            "long_url": None,
            "qr_code": None,
            "copy_paste": None,
            "expires_at": None,
            "raw_action": next_action,
        }

        # 1. 重定向跳转型 (iDEAL / UPI / 部分 Kakao Pay)
        if action_type == "redirect_to_url" or next_action.get("redirect_to_url"):
            redirect_info = next_action.get("redirect_to_url") or {}
            out["url"] = redirect_info.get("url")
            out["long_url"] = redirect_info.get("url")
            logger.info("[Stripe-LPM] 🎉 成功捕获 %s 跳转直链: %s", self.spec["name"], out["url"][:60])
            return out

        # 2. 二维码/凭证展示型 (PIX)
        if action_type == "display_pix_qr_code" or next_action.get("display_pix_qr_code"):
            pix_info = next_action.get("display_pix_qr_code") or {}
            out["url"] = pix_info.get("hosted_instructions_url") or data.get("url")
            out["long_url"] = out["url"]
            out["qr_code"] = pix_info.get("data_url") or pix_info.get("image_url_png") or pix_info.get("image_url_svg")
            out["copy_paste"] = pix_info.get("copia_e_cola") or pix_info.get("hosted_instructions_url") or pix_info.get("data_url")
            out["expires_at"] = pix_info.get("expires_at")
            logger.info("[Stripe-LPM] 🎉 成功捕获 PIX 付款指引外链: %s", (out["url"] or "")[:60])
            return out

        # 3. 兜底提取顶层 url
        top_url = data.get("url") or data.get("redirect_url")
        if top_url:
            out["url"] = top_url
            out["long_url"] = top_url
            return out

        raise RuntimeError(f"未能从 confirm 响应中识别出有效的支付跳转行为: {str(next_action)[:200]}")
