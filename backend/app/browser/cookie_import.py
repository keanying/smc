"""把用户从浏览器里导出的 Cookie 解析成统一格式。

为什么需要手动导入：
    扫码登录会失败、会过期，服务器上跑的浏览器还可能被平台风控拦下。
    用户自己在日常浏览器里登录好，用 Cookie-Editor 之类的插件导出，
    贴进来是最直接可靠的一条路——尤其是抖音、快手这种登录门槛高的平台。

支持三种粘贴内容，自动识别：

1. Cookie-Editor 的 JSON 导出（"Export" 按钮，数组形式）
   [{"name":"sessionid","value":"...","domain":".douyin.com",
     "path":"/","expirationDate":1792748274.5,"httpOnly":true,
     "secure":true,"sameSite":"no_restriction","hostOnly":false,"session":false}, ...]

2. Cookie-Editor 的 "Export as Header String" / 浏览器 devtools 里的 Cookie 请求头
   name=value; name2=value2
   （分号后有没有空格都认；值里含 = 的按第一个 = 切）

3. document.cookie 的输出——和第 2 种同构

输出统一是 Playwright 的 cookie 结构，可以直接喂给
`BrowserContext.add_cookies()`，也可以直接存进 src_opinion_social_account.cookies。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.logging import get_logger

logger = get_logger(__name__)

#: Cookie-Editor 的 sameSite 取值 -> Playwright 的取值
_SAME_SITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}

#: 明显不是登录凭据、贴进来只会添乱的键（体积大且每次访问都会变）
_NOISE_KEYS = {"__ac_nonce", "__ac_signature", "biz_trace_id"}


class CookieParseError(ValueError):
    """粘贴的内容解析不出任何 Cookie。"""


#: 二级公共后缀：这些前面还得再带一级才是真正的注册域
_MULTI_LABEL_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.uk", "org.uk", "co.jp", "com.hk", "com.tw",
}


def default_domain(home_url: str) -> str:
    """从站点首页推出 Cookie 该挂在哪个域上。

    https://www.douyin.com  -> .douyin.com
    https://m.weibo.cn      -> .weibo.cn
    http://127.0.0.1:8080   -> 127.0.0.1   （IP 和 localhost 不能带前导点，
                                            带了 Chromium 会以 Invalid cookie fields 拒收）
    只有 header 串（不带域信息）时用它兜底。
    """
    host = (urlparse(home_url).hostname or "").strip().lower()
    if not host:
        return ""

    # IP 地址（含 IPv6 的 ::1）和单标签主机名整个用，不加点
    if _is_ip(host) or "." not in host:
        return host

    parts = [p for p in host.split(".") if p]
    tail_two = ".".join(parts[-2:])
    take = 3 if (tail_two in _MULTI_LABEL_SUFFIXES and len(parts) >= 3) else 2
    return "." + ".".join(parts[-take:])


def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse(raw: str, *, fallback_domain: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """解析粘贴内容，返回 (cookie 列表, 识别到的格式)。"""
    text = (raw or "").strip()
    if not text:
        raise CookieParseError("粘贴内容是空的")

    if text.startswith("[") or text.startswith("{"):
        return _parse_json(text, fallback_domain), "json"
    return _parse_header(text, fallback_domain), "header"


def _parse_json(text: str, fallback_domain: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CookieParseError(
            f"看起来是 JSON 但解析失败：{exc}。"
            f"请确认复制的是 Cookie-Editor「Export」出来的完整数组。"
        ) from exc

    if isinstance(data, dict):
        # 有的插件包一层 {"cookies": [...]}；也可能是 {"name": "value"} 这种简单映射
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        else:
            data = [{"name": k, "value": v} for k, v in data.items()]
    if not isinstance(data, list):
        raise CookieParseError("JSON 里没有 Cookie 数组")

    cookies: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name in _NOISE_KEYS:
            continue
        value = entry.get("value")
        if value is None:
            continue

        cookie: Dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": str(entry.get("domain") or fallback_domain or "").strip(),
            "path": str(entry.get("path") or "/"),
        }
        if not cookie["domain"]:
            raise CookieParseError(
                f"Cookie「{name}」没有 domain，也没有可用的兜底域名——"
                f"请确认导出的是完整 JSON 而不是精简过的"
            )

        # hostOnly 的 Cookie 域名不能带前导点，Playwright 会拒绝
        if entry.get("hostOnly") and cookie["domain"].startswith("."):
            cookie["domain"] = cookie["domain"][1:]

        if entry.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(entry["httpOnly"])
        if entry.get("secure") is not None:
            cookie["secure"] = bool(entry["secure"])

        same_site = str(entry.get("sameSite") or "").lower()
        if same_site in _SAME_SITE_MAP:
            cookie["sameSite"] = _SAME_SITE_MAP[same_site]

        expires = _expiry(entry)
        if expires is not None:
            cookie["expires"] = expires

        cookies.append(cookie)

    if not cookies:
        raise CookieParseError("JSON 解析成功，但里面一条有效 Cookie 都没有")
    return cookies


def _expiry(entry: Dict[str, Any]) -> Optional[float]:
    """Cookie-Editor 用 expirationDate（浮点秒），Playwright 用 expires。

    会话 Cookie（session=true 或没有过期时间）不写 expires，
    让它跟着浏览器上下文走。
    """
    if entry.get("session"):
        return None
    for key in ("expirationDate", "expires", "expiry", "expiration_date"):
        value = entry.get(key)
        if value in (None, "", 0, -1):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        # 有的导出用毫秒
        if number > 10_000_000_000:
            number /= 1000
        return number
    return None


def _parse_header(text: str, fallback_domain: str) -> List[Dict[str, Any]]:
    if not fallback_domain:
        raise CookieParseError(
            "粘贴的是 name=value 形式的 Cookie 串，但没有域名信息。"
            "请选择正确的平台后再导入。"
        )

    # 用户从 devtools 复制时可能带上 "Cookie: " 前缀；换行也当分隔符处理
    body = text
    for prefix in ("cookie:", "Cookie:", "COOKIE:"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    body = body.replace("\n", ";").replace("\r", "")

    cookies: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in body.split(";"):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        # 值里面可能还有 =（base64 padding），只按第一个切
        name, _, value = pair.partition("=")
        name = name.strip()
        if not name or name in _NOISE_KEYS or name in seen:
            continue
        seen.add(name)
        cookies.append({
            "name": name,
            "value": value.strip(),
            "domain": fallback_domain,
            "path": "/",
        })

    if not cookies:
        raise CookieParseError(
            "没解析出任何 Cookie。期望的格式是 name=value; name2=value2，"
            "或者 Cookie-Editor 导出的 JSON 数组。"
        )
    return cookies


#: 没有过期时间的 Cookie 默认给多久
DEFAULT_TTL_DAYS = 30


def ensure_persistent(
    cookies: List[Dict[str, Any]], *, ttl_days: int = DEFAULT_TTL_DAYS
) -> List[Dict[str, Any]]:
    """给没有过期时间的 Cookie 补一个，否则它们进不了浏览器 profile。

    ⚠️ 这一步不能省。会话 Cookie（没有 expires）**只活在内存里**，
    Chromium 根本不会把它们写进 profile 的 Cookies 库。
    而用户粘 `name=value; name2=value2` 这种请求头串时，
    里面一个过期时间都没有——于是导入"成功"了，
    下次采集打开 profile 却一个 Cookie 都没有，
    表现和没导入一模一样，非常难查。

    导入的语义本来就是"我要让这个登录态留下来"，所以统一补成持久 Cookie。
    """
    import time

    deadline = time.time() + ttl_days * 86400
    result = []
    for cookie in cookies:
        item = dict(cookie)
        if not item.get("expires"):
            item["expires"] = deadline
        result.append(item)
    return result


def cookie_matches_host(cookie: Dict[str, Any], host: str) -> bool:
    """这条 Cookie 会不会被发给 host？按浏览器的规则来。

    `.weibo.com` 匹配 weibo.com 和它的子域；`m.weibo.cn` 只匹配自己。
    没写 domain 的（手动导入的请求头串可能这样）一律放行——
    宁可多发一个，也不要因为缺字段把登录凭据丢掉。
    """
    domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
    host = (host or "").strip().lower()
    if not domain:
        return True
    return host == domain or host.endswith("." + domain)


def header_for_host(cookies: List[Dict[str, Any]], host: str) -> str:
    """拼出发给某个站点的 Cookie 请求头。

    ⚠️ 必须按域过滤，不能把手里所有 Cookie 一股脑发出去。
    微博是最典型的例子：`.weibo.com` 和 `.weibo.cn` **各有一个叫 SUB 的 Cookie**，
    值完全不同、互不通用。不过滤的话两个 SUB 会一起进请求头，
    去重时留下的可能正好是另一个域那份 —— 平台判定未登录，
    而报错里只会说"登录态失效"，完全看不出是发错了 Cookie。
    """
    return to_header([c for c in cookies if cookie_matches_host(c, host)])


def to_header(cookies: List[Dict[str, Any]]) -> str:
    """把 Cookie 列表拼成请求头。**同名的只留最后一个。**

    ⚠️ 为什么要去重：同一个 name 会在多个域上各存一份——
    `.douyin.com` 和 `www.douyin.com` 都能匹配 https://www.douyin.com，
    于是 `context.cookies(...)` 会把 `ttwid` 之类返回两遍。
    直接 join 出来的头长这样：

        ttwid=A; ...; ttwid=B

    真实浏览器绝不会这么发。服务端取第一个还是最后一个各家不一样，
    于是就出现"同一份 Cookie，有时候能采有时候不能"这种最难查的现象。
    取最后一个：Playwright 返回的顺序里，域更具体的通常在后面，
    也就是浏览器自己会优先发的那一份。
    """
    merged: Dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        merged[str(name)] = str(value)
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def summarize(cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
    """给前端回一份可读的导入结果。"""
    domains = sorted({c.get("domain", "") for c in cookies if c.get("domain")})
    return {
        "count": len(cookies),
        "domains": domains,
        "names": sorted(c["name"] for c in cookies),
    }
