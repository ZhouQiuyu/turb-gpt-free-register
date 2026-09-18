# -*- coding: utf-8 -*-
"""
国别与地理信息处理工具库。
提供 ISO-3166-1 二字码转换国旗 Emoji、中文国家名称映射及格式化展示。
"""
from __future__ import annotations

# 常见国家及地区 ISO 2字码到中文全称的映射
COUNTRY_CODE_TO_CN: dict[str, str] = {
    "JP": "日本",
    "US": "美国",
    "HK": "中国香港",
    "TW": "中国台湾",
    "SG": "新加坡",
    "GB": "英国",
    "UK": "英国",
    "DE": "德国",
    "FR": "法国",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "KR": "韩国",
    "NL": "荷兰",
    "IN": "印度",
    "MY": "马来西亚",
    "TH": "泰国",
    "VN": "越南",
    "PH": "菲律宾",
    "ID": "印尼",
    "BR": "巴西",
    "RU": "俄罗斯",
    "IT": "意大利",
    "ES": "西班牙",
    "SE": "瑞典",
    "CH": "瑞士",
    "TR": "土耳其",
    "AE": "阿联酋",
    "CN": "中国",
    "MX": "墨西哥",
    "ZA": "南非",
    "IE": "爱尔兰",
    "PL": "波兰",
    "NO": "挪威",
    "FI": "芬兰",
    "DK": "丹麦",
    "NZ": "新西兰",
    "AR": "阿根廷",
    "CL": "智利",
    "CO": "哥伦比亚",
    "IL": "以色列",
    "SA": "沙特阿拉伯",
    "EG": "埃及",
    "NG": "尼日利亚",
    "UA": "乌克兰",
    "CZ": "捷克",
    "AT": "奥地利",
    "BE": "比利时",
    "PT": "葡萄牙",
    "GR": "希腊",
    "RO": "罗马尼亚",
    "HU": "匈牙利",
}


def get_country_flag(country_code: str | None) -> str:
    """根据 ISO-3166-1 alpha-2 代码生成国旗 Emoji。"""
    c = str(country_code or "").strip().upper()
    if len(c) != 2 or not c.isalpha():
        return "🌐"
    return "".join(chr(127397 + ord(char)) for char in c)


def get_country_cn_name(country_code: str | None, fallback: str = "") -> str:
    """获取国家或地区中文名称。"""
    c = str(country_code or "").strip().upper()
    if c in COUNTRY_CODE_TO_CN:
        return COUNTRY_CODE_TO_CN[c]
    if fallback:
        return str(fallback).strip()
    return "未知地区" if not c else c


def format_country_badge(country_code: str | None, fallback_country: str = "") -> str:
    """格式化标准展示徽标，如：🇯🇵 日本 (JP) 或 🌐 未知 (UN)。"""
    code = str(country_code or "").strip().upper()
    if not code or len(code) != 2:
        return "🌐 未知 (UN)"
    flag = get_country_flag(code)
    name_cn = get_country_cn_name(code, fallback=fallback_country)
    return f"{flag} {name_cn} ({code})"


def get_country_badge_info(country_code: str | None, fallback_country: str = "") -> dict[str, str]:
    """返回国别展示详情字典。"""
    code = str(country_code or "").strip().upper()
    if not code or len(code) != 2:
        return {
            "code": "",
            "flag": "🌐",
            "name_cn": "未知地区",
            "badge": "🌐 未知 (UN)",
        }
    flag = get_country_flag(code)
    name_cn = get_country_cn_name(code, fallback=fallback_country)
    return {
        "code": code,
        "flag": flag,
        "name_cn": name_cn,
        "badge": f"{flag} {name_cn} ({code})",
    }
