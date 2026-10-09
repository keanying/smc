"""设备指纹画像。

一个画像是一整套自洽的请求头，而不是单独换个 UA：
  - Safari 画像不带 sec-ch-ua / Sec-Fetch-*（Client Hints 是 Chromium 独有）
  - Chrome 画像的 sec-ch-ua 版本号必须和 UA 里的版本号一致
  - Accept-Language 的写法按浏览器习惯区分（Safari 用 zh-Hans）
一个画像绑定一个出口 IP，两者同生共死，不做单请求级随机。

移动端画像用于携程、同程、微博 m 站；桌面端画像用于抖音、快手、小红书 web。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

# ---------------- 移动端 ----------------
MOBILE_PROFILES: List[Dict] = [
    {
        "name": "iOS17-Safari",
        "platform": "mobile",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        },
    },
    {
        "name": "iOS18-Safari",
        "platform": "mobile",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        },
    },
    {
        "name": "iOS18-Safari-iPad",
        "platform": "mobile",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        },
    },
    {
        "name": "Android14-Chrome134",
        "platform": "mobile",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; PJD110 Build/UKQ1.230924.001) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        },
    },
    {
        "name": "Android13-Chrome131",
        "platform": "mobile",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; V2266A Build/TP1A.220905.001) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        },
    },
]

# ---------------- 桌面端 ----------------
DESKTOP_PROFILES: List[Dict] = [
    {
        "name": "Win11-Chrome131",
        "platform": "desktop",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    },
    {
        "name": "Win11-Chrome134",
        "platform": "desktop",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    },
    {
        "name": "macOS-Chrome133",
        "platform": "desktop",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="133", "Not_A Brand";v="24", "Google Chrome";v="133"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    },
]

ALL_PROFILES: List[Dict] = MOBILE_PROFILES + DESKTOP_PROFILES
_BY_NAME = {p["name"]: p for p in ALL_PROFILES}

# 只有装了 brotli 才敢声明 br，否则响应解不开
try:  # pragma: no cover - 取决于运行环境
    import brotli  # noqa: F401
    _ACCEPT_ENCODING = "gzip, deflate, br"
except ImportError:  # pragma: no cover
    try:
        import brotlicffi  # noqa: F401
        _ACCEPT_ENCODING = "gzip, deflate, br"
    except ImportError:
        _ACCEPT_ENCODING = "gzip, deflate"

COMMON_HEADERS: Dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Encoding": _ACCEPT_ENCODING,
}

# 发送顺序对齐真实浏览器的 XHR 请求（httpx 按插入顺序发送）
HEADER_ORDER = [
    "sec-ch-ua",
    "Content-Type",
    "sec-ch-ua-mobile",
    "User-Agent",
    "sec-ch-ua-platform",
    "Accept",
    "Origin",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
]


def pick_profile(name: Optional[str] = None, platform: Optional[str] = None) -> Dict:
    """按名字取指定画像；否则在指定端（mobile/desktop）里随机取一个。"""
    if name and name in _BY_NAME:
        return _BY_NAME[name]
    if platform == "mobile":
        return random.choice(MOBILE_PROFILES)
    if platform == "desktop":
        return random.choice(DESKTOP_PROFILES)
    return random.choice(ALL_PROFILES)


def build_headers(profile: Dict, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """把画像展开成完整请求头，并按浏览器习惯排序。"""
    merged = dict(COMMON_HEADERS)
    merged.update(profile.get("headers", {}))
    if extra:
        merged.update(extra)
    headers = {key: merged[key] for key in HEADER_ORDER if key in merged}
    for key, value in merged.items():  # 兜底：不在顺序表里的自定义头
        headers.setdefault(key, value)
    return headers
