"""图片代理。

## 为什么需要它

各平台的图片 CDN（微博 sinaimg、抖音 douyinpic、小红书 xhscdn、快手 kwimgs…）
都做了**防盗链**：请求头里带着别的站点的 Referer 就回 403。
表现极具迷惑性——把图片地址复制到浏览器地址栏能正常打开（那样不带 Referer），
可页面里就是一片"加载失败"，让人以为是采到的地址不对。

前端加 `<meta name="referrer" content="no-referrer">` 能解决一部分，但不够：

  1. 有的 CDN 不是"不要 Referer"，而是"必须是自家域名的 Referer"，
     不发反而更糟
  2. 浏览器扩展、企业策略、上层网关都可能覆盖页面的 referrer policy
  3. 部分 CDN 还会看 User-Agent

所以最终由服务端代取：想发什么头就发什么头，浏览器那边只是一个同源请求，
不受任何 referrer policy 影响。

## ⚠️ 这是一个"按 URL 取内容"的接口，天生有 SSRF 风险

必须守住三条：
  1. **只允许 http/https**，挡掉 file:// gopher:// 之类
  2. **域名白名单**，只放行已知的图片 CDN——绝不能变成一个通用的对外代理，
     否则内网地址、云厂商元数据接口（169.254.169.254）都能被读出来
  3. **响应必须是图片**，且有大小上限
"""
from __future__ import annotations

import asyncio
import ipaddress
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from .deps import AppState, get_state
from ..core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/media", tags=["媒体"])

#: 允许代理的域名后缀 -> 请求时伪装成哪个站点来的。
#: 空字符串表示不发 Referer（大多数 CDN 认这个）。
ALLOWED_HOSTS = {
    # 微博
    "sinaimg.cn": "https://weibo.com/",
    "weibocdn.com": "https://weibo.com/",
    # 抖音 / 字节系
    "douyinpic.com": "https://www.douyin.com/",
    "byteimg.com": "https://www.douyin.com/",
    "pstatp.com": "https://www.douyin.com/",
    "bytecdn.cn": "https://www.douyin.com/",
    "ixigua.com": "https://www.douyin.com/",
    # 小红书
    "xhscdn.com": "https://www.xiaohongshu.com/",
    # 快手
    "kwimgs.com": "https://www.kuaishou.com/",
    "yximgs.com": "https://www.kuaishou.com/",
    "kwaicdn.com": "https://www.kuaishou.com/",
    # 携程
    "c-ctrip.com": "",
    "tripcdn.com": "",
    "ctrip.com": "",
    # 同程
    "40017.cn": "",
    "tcimg.com": "",
    "ly.com": "",
}

#: 单张图最大 20MB，超过直接拒——代理不是用来传大文件的
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 20

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)


def _referer_for(host: str) -> Optional[str]:
    """域名在白名单里就返回该发的 Referer（可能是空串），不在返回 None。"""
    host = (host or "").lower().strip(".")
    for suffix, referer in ALLOWED_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return referer
    return None


def _is_public_host(host: str) -> bool:
    """挡掉直接写 IP 指向内网的情况。

    域名形式的内网地址挡不住（那要做 DNS 解析，代价和风险都更大），
    但白名单已经把域名限死了，这里只是对"白名单里塞了个 IP"的兜底。
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True     # 不是 IP，交给白名单判断
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast
    )


class _ImageCache:
    """服务端图片缓存（进程内，LRU + 总量上限）。

    为什么需要：一页 20 条作品，小红书一条笔记 9~18 张图，
    一次翻页就能触发 200+ 次代理请求。没有缓存的话，用户来回翻页、
    或者两个人同时看同一批数据，同样的图要从平台 CDN 反复取。

    只缓存小图（缩略图基本都在几十 KB），大图直接放过——
    它们占内存不划算，而且通常只看一次。
    """

    #: 单张图超过这个大小就不缓存了
    MAX_ITEM_BYTES = 512 * 1024
    #: 整个缓存最多占这么多内存
    MAX_TOTAL_BYTES = 64 * 1024 * 1024

    def __init__(self) -> None:
        self._items: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._total = 0

    def get(self, url: str):
        item = self._items.get(url)
        if item is None:
            return None
        self._items.move_to_end(url)      # LRU：刚用过的挪到末尾
        return item

    def put(self, url: str, content: bytes, content_type: str) -> None:
        if len(content) > self.MAX_ITEM_BYTES:
            return
        if url in self._items:
            self._total -= len(self._items[url][0])
        self._items[url] = (content, content_type)
        self._items.move_to_end(url)
        self._total += len(content)
        while self._total > self.MAX_TOTAL_BYTES and self._items:
            _, (evicted, _ct) = self._items.popitem(last=False)
            self._total -= len(evicted)


_CACHE = _ImageCache()

#: 全进程共用一个 httpx 客户端。
#: 原来是每张图 `async with httpx.AsyncClient(...)` 新建一个——
#: 每次都要重新 TCP 握手 + TLS 握手，200 张图就是 200 次完整握手，
#: 全压在同一个事件循环上，还要和数据库查询抢 CPU。
#: 共用一个客户端就能复用连接池（keep-alive）。
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=50, max_keepalive_connections=20,
                        keepalive_expiry=30.0,
                    ),
                )
    return _client


async def close_media_client() -> None:
    """服务关闭时收尾。不关的话 httpx 会在退出时抱怨连接没释放。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@router.get("/image")
async def proxy_image(
    url: str = Query(..., description="平台图片地址"),
    state: AppState = Depends(get_state),
):
    """代取一张平台图片，绕开防盗链。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "只支持 http/https 地址")
    host = parsed.hostname or ""
    referer = _referer_for(host)
    if referer is None or not _is_public_host(host):
        # 不在白名单里的一律拒绝。这个接口只服务于"显示采到的图片"，
        # 放开成通用代理就等于把内网暴露出去了。
        raise HTTPException(400, f"不支持代理这个域名：{host}")

    cached = _CACHE.get(url)
    if cached is not None:
        content, content_type = cached
        return _image_response(content, content_type, hit=True)

    headers = {"User-Agent": _UA, "Accept": "image/*,*/*"}
    if referer:
        headers["Referer"] = referer

    try:
        client = await _get_client()
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.debug("代理图片失败 %s：%s", url, exc)
        raise HTTPException(502, f"取图片失败：{exc}") from exc

    if response.status_code != 200:
        raise HTTPException(
            502, f"平台返回 HTTP {response.status_code}（图片可能已被删除）"
        )
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(502, f"返回的不是图片：{content_type or '未知类型'}")
    if len(response.content) > MAX_BYTES:
        raise HTTPException(413, "图片过大")

    _CACHE.put(url, response.content, content_type)
    return _image_response(response.content, content_type, hit=False)


def _image_response(content: bytes, content_type: str, *, hit: bool) -> Response:
    return Response(
        content=content,
        media_type=content_type,
        headers={
            # 图片内容不会变，让浏览器缓存一天，别每次翻页都回源
            "Cache-Control": "public, max-age=86400",
            # 排障用：能一眼看出这张图是服务端缓存给的还是回源取的
            "X-Media-Cache": "hit" if hit else "miss",
        },
    )
