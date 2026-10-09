"""代理管理器 + 带自动轮换的 HTTP 客户端。

所有平台的采集器都通过 ProxiedClient 发请求，从而天然获得：
  - 出口 IP 轮换（TTL 到期 / 被限流 / 连接失败）
  - 出口 IP 与设备指纹绑定
  - 统一的超时、重试、限速

代理未启用时 ProxiedClient 退化为普通 httpx 客户端，采集逻辑不用改一行。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

from urllib.parse import urlencode

import httpx

from ..core.config import Config
from ..core.logging import get_logger
from ..core.redis_client import RedisClient
from .fingerprint import build_headers, pick_profile
from .pool import PROXY_BAD_STATUS, KdlProxyPool, ProxyBlocked, ProxyUnavailable

logger = get_logger(__name__)

# 各平台默认用哪一端的指纹
CHANNEL_FINGERPRINT_PLATFORM = {
    "ctrip": "mobile",
    "tongcheng": "mobile",
    # ⚠️ 微博必须走 desktop：参考项目 weibo_opinion 的 PC 模式就是桌面 Chrome 头，
    # 移动端指纹会被 s.weibo.com 风控，返回空搜索页。
    "weibo": "desktop",
    "douyin": "desktop",
    "kuaishou": "desktop",
    "xiaohongshu": "desktop",
}


class ProxyManager:
    """按平台维护代理池；配置里 pool_scope=global 时全平台共用一个池。"""

    def __init__(self, config: Config, redis: RedisClient):
        self._config = config
        self._redis = redis
        self._pools: Dict[str, KdlProxyPool] = {}
        self._lock = asyncio.Lock()

    def enabled_for(self, channel: str) -> bool:
        return bool(self._config.proxy_for(channel).get("enabled"))

    async def get_pool(self, channel: str) -> Optional[KdlProxyPool]:
        cfg = self._config.proxy_for(channel)
        if not cfg.get("enabled"):
            return None

        scope = channel if cfg.get("pool_scope", "channel") == "channel" else "global"
        if scope in self._pools:
            return self._pools[scope]

        async with self._lock:
            if scope in self._pools:
                return self._pools[scope]
            pool = KdlProxyPool(
                self._redis,
                scope=scope,
                secret_id=cfg.get("secret_id", ""),
                secret_key=cfg.get("secret_key", ""),
                username=cfg.get("username", ""),
                password=cfg.get("password", ""),
                auth_mode=cfg.get("auth_mode", "token"),
                min_ttl_seconds=cfg.get("min_ttl_seconds", 1200),
                max_ttl_seconds=cfg.get("max_ttl_seconds", 1800),
                safety_buffer_seconds=cfg.get("safety_buffer_seconds", 30),
                fetch_retries=cfg.get("fetch_retries", 5),
                validate_on_fetch=cfg.get("validate_on_fetch", True),
                api_timeout_seconds=cfg.get("api_timeout_seconds", 10),
                fingerprint_platform=CHANNEL_FINGERPRINT_PLATFORM.get(channel),
            )
            self._pools[scope] = pool
            return pool

    def reset(self) -> None:
        """系统设置里改过代理配置后调用，下次取用会按新配置重建池。"""
        self._pools.clear()

    async def status(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for scope, pool in self._pools.items():
            result[scope] = {
                "current_ip": pool.current_ip(),
                "remaining_ttl": pool.remaining_ttl(),
            }
        return result


class ProxiedClient:
    """一个采集会话用一个实例：持有当前 IP + 指纹，失败自动换 IP 重试。

    用法：
        async with ProxiedClient(config, proxy_manager, "ctrip") as client:
            data = await client.request_json("POST", url, json_body=payload)
    """

    def __init__(
        self,
        config: Config,
        manager: ProxyManager,
        channel: str,
        *,
        base_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        logger_override: Any = None,
    ):
        self._config = config
        self._manager = manager
        self.channel = channel
        self._base_headers = base_headers or {}
        self._timeout = float(timeout or config.get("crawl.request_timeout_seconds", 25))
        self._max_retries = int(max_retries or config.get("crawl.max_retries", 3))
        self._rotate_every = int(config.proxy_for(channel).get("rotate_every_n_requests", 0))
        self._log = logger_override or logger

        self._pool: Optional[KdlProxyPool] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._profile: Dict = pick_profile(platform=CHANNEL_FINGERPRINT_PLATFORM.get(channel))
        self._proxy_url: Optional[str] = None
        self._request_count = 0
        #: 最近一次响应的摘要，「试搜」诊断用
        self.last_response: Dict[str, Any] = {}

    # ---------------- 生命周期 ----------------
    async def __aenter__(self) -> "ProxiedClient":
        self._pool = await self._manager.get_pool(self.channel)
        await self._rebuild_client()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _rebuild_client(self, force_new_ip: bool = False) -> None:
        """重建底层 httpx 客户端：换 IP 必然同时换指纹。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

        if self._pool is not None:
            self._proxy_url, self._profile = await self._pool.get_bundle(force_new=force_new_ip)
        else:
            self._proxy_url = None
            self._profile = pick_profile(platform=CHANNEL_FINGERPRINT_PLATFORM.get(self.channel))

        self._client = httpx.AsyncClient(
            proxy=self._proxy_url,
            timeout=self._timeout,
            follow_redirects=True,
            headers=build_headers(self._profile, self._base_headers),
        )
        self._request_count = 0

    def set_header(self, name: str, value: str) -> None:
        """改一个请求头，并让已经建好的底层客户端立刻生效。

        用途：采集途中登录态失效，重新开浏览器刷了 Cookie 之后要换头。
        只改 _base_headers 是不够的——那只在 _rebuild_client 时才会被读，
        不换 IP 就一直用着旧 Cookie，表现是"刷新了也还是提示未登录"。
        """
        self._base_headers[name] = value
        if self._client is not None:
            self._client.headers[name] = value

    # ---------------- 属性 ----------------
    @property
    def profile_name(self) -> str:
        return self._profile.get("name", "")

    @property
    def proxy_url(self) -> Optional[str]:
        return self._proxy_url

    @property
    def headers(self) -> httpx.Headers:
        assert self._client is not None
        return self._client.headers

    def set_header(self, key: str, value: str) -> None:
        """设置会话级请求头（如 Cookie、Referer）；重建客户端后仍保留。"""
        self._base_headers[key] = value
        if self._client is not None:
            self._client.headers[key] = value

    async def rotate(self, reason: str = "主动轮换") -> None:
        """主动换一个出口 IP 与指纹。"""
        if self._pool is not None:
            self._pool.mark_invalid(reason)
        await self._rebuild_client(force_new_ip=False)

    # ---------------- 请求 ----------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict] = None,
        json_body: Any = None,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        expect_json: bool = True,
    ) -> httpx.Response:
        """发请求；代理类失败自动换 IP 重试，业务类失败原样抛出交给调用方判断。"""
        assert self._client is not None, "请在 async with 语句内使用 ProxiedClient"
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            # 按次数主动轮换
            if self._rotate_every and self._request_count >= self._rotate_every:
                await self._rebuild_client(force_new_ip=True)

            try:
                self._request_count += 1
                response = await self._client.request(
                    method, url, params=params, json=json_body, content=data, headers=headers,
                )
            except httpx.HTTPError as exc:
                last_error = ProxyBlocked(f"网络/代理异常：{exc}")
                self._log.warning(
                    "[%s] 第 %d/%d 次请求失败（%s），换 IP 重试",
                    self.channel, attempt, self._max_retries, exc,
                )
                await self._handle_blocked(str(exc))
                await asyncio.sleep(min(1 + attempt, 5))
                continue

            if response.status_code in PROXY_BAD_STATUS:
                last_error = ProxyBlocked(
                    f"HTTP {response.status_code}，判定当前出口 IP 被限制"
                )
                self._log.warning(
                    "[%s] 第 %d/%d 次请求返回 %d，判定 IP 被限制，换 IP 重试",
                    self.channel, attempt, self._max_retries, response.status_code,
                )
                await self._handle_blocked(f"HTTP {response.status_code}")
                await asyncio.sleep(min(1 + attempt, 5))
                continue

            if expect_json and response.text.strip() == "blocked":
                last_error = ProxyBlocked("响应体为 blocked，疑似风控")
                await self._handle_blocked("响应 blocked")
                await asyncio.sleep(min(1 + attempt, 5))
                continue

            response.raise_for_status()
            return response

        raise last_error or RuntimeError(f"请求失败：{url}")

    async def _handle_blocked(self, reason: str) -> None:
        if self._pool is not None:
            self._pool.mark_invalid(reason)
            await self._rebuild_client(force_new_ip=False)
        # 无代理模式下换个指纹并退避，聊胜于无
        else:
            self._profile = pick_profile(
                platform=CHANNEL_FINGERPRINT_PLATFORM.get(self.channel)
            )
            assert self._client is not None
            self._client.headers.update(build_headers(self._profile, self._base_headers))

    async def request_json(self, method: str, url: str, **kwargs) -> Any:
        response = await self.request(method, url, **kwargs)
        # 留一份最近一次响应的摘要，供「试搜」诊断用。
        # 平台静默失败（200 + 空 data）时，没有这份摘要就完全无从下手。
        self.last_response = {
            "url": str(response.url).split("?")[0],
            "status": response.status_code,
            "length": len(response.text or ""),
            "snippet": (response.text or "")[:600],
        }
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"JSON 解析失败：{url}，响应前 200 字符：{response.text[:200]}"
            ) from exc

    @staticmethod
    def build_url(url: str, params: Optional[Dict[str, Any]] = None) -> str:
        """把 url + params 拼成人看得懂的完整地址，**只用于报错和日志**。

        排查采集问题时，"接口 /comments/hotflow 报错"这种信息几乎没用——
        同一个接口一次任务里要调几百次，看不出是哪条作品、翻到第几页。
        带上查询串就能直接复制到浏览器里重放。
        """
        if not params:
            return url
        try:
            return f"{url}?{urlencode(params, doseq=True)}"
        except Exception:  # noqa: BLE001
            return url

    async def get_json(self, url: str, **kwargs) -> Any:
        return await self.request_json("GET", url, **kwargs)

    async def post_json(self, url: str, **kwargs) -> Any:
        return await self.request_json("POST", url, **kwargs)

    async def get_text(self, url: str, **kwargs) -> str:
        kwargs.setdefault("expect_json", False)
        response = await self.request("GET", url, **kwargs)
        return response.text

    async def sleep_interval(self) -> None:
        """按配置的采集间隔休眠，带 ±20% 抖动避免整点齐发。"""
        base = float(self._config.get("crawl.request_interval_seconds", 1.0))
        if base <= 0:
            return
        await asyncio.sleep(base * random.uniform(0.8, 1.2))
