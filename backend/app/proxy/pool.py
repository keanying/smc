"""快代理私密代理 IP 池（异步版，全平台公用）。

来源于用户已验证可用的携程采集脚本，做了三点通用化改造：
  1. 同步 requests -> 异步 httpx，适配整个服务的 async 架构
  2. Redis key 按平台隔离（pool_scope=channel），一个平台的 IP 被封
     不会连累其他平台；也可切成 global 让全平台共用一个 IP 省提取次数
  3. 代理与设备指纹绑定后一起轮换，避免"换了 IP 没换指纹"

策略要点（沿用原脚本）：
  - 每次只持有一个"当前 IP"，写在 Redis 里带 TTL，到期自动失效换新
  - 连接异常 / 超时 / 403 / 407 / 429 / 5xx 判定为当前 IP 不可用，立即弃用
  - 多进程共用同一 key 时用 SET NX 锁，避免并发重复提取浪费提取次数
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from ..core.logging import get_logger
from ..core.redis_client import RedisClient
from .fingerprint import build_headers, pick_profile

logger = get_logger(__name__)

PROXY_LINE_RE = re.compile(r"^\s*([\d.]+:\d+)\s*(?:,\s*(\d+(?:\.\d+)?))?\s*$")

# 出现这些状态码时认为是出口 IP 被限制，而不是业务错误
PROXY_BAD_STATUS = {403, 407, 429, 502, 503, 504}


class ProxyUnavailable(RuntimeError):
    """无法从快代理拿到可用 IP。"""


class ProxyBlocked(RuntimeError):
    """当前出口 IP 不可用（连接失败 / 被限流 / 被封）。"""


class AuthRejected(RuntimeError):
    """提取接口的签名/令牌被拒绝，需要重新取 token。"""


class KdlProxyPool:
    """一个 pool 实例管一个 Redis key 下的"当前 IP"。"""

    API_URL = "https://dps.kdlapi.com/api/getdps/"
    AUTH_TOKEN_URL = "https://auth.kdlapi.com/api/get_secret_token"
    TEST_URL = "https://dev.kdlapi.com/testproxy"

    def __init__(
        self,
        redis: RedisClient,
        *,
        scope: str = "global",
        secret_id: str = "",
        secret_key: str = "",
        username: str = "",
        password: str = "",
        auth_mode: str = "token",
        min_ttl_seconds: int = 20 * 60,
        max_ttl_seconds: int = 30 * 60,
        safety_buffer_seconds: int = 30,
        fetch_retries: int = 5,
        validate_on_fetch: bool = True,
        api_timeout_seconds: int = 10,
        fingerprint_platform: Optional[str] = None,
    ):
        self.redis = redis
        self.scope = scope
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.username = username
        self.password = password
        self.auth_mode = (auth_mode or "token").lower()
        if self.auth_mode not in ("token", "plain"):
            raise ValueError("auth_mode 只支持 token（密钥令牌验证）或 plain（明文验证）")
        self.min_ttl_seconds = int(min_ttl_seconds)
        self.max_ttl_seconds = int(max_ttl_seconds)
        self.safety_buffer_seconds = int(safety_buffer_seconds)
        self.fetch_retries = int(fetch_retries)
        self.validate_on_fetch = bool(validate_on_fetch)
        self.api_timeout = int(api_timeout_seconds)
        self.fingerprint_platform = fingerprint_platform

        self.current_key = redis.key("proxy", scope, "current")
        self.lock_key = redis.key("proxy", scope, "lock")
        # token 全局共用：同一订单的 secret_token 与平台无关
        self.token_key = redis.key("proxy", "token")
        self._async_lock = asyncio.Lock()

    # ---------------- 读写当前 IP ----------------
    def _read_bundle(self) -> Optional[Dict]:
        raw = self.redis.get(self.current_key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            self.redis.delete(self.current_key)
            return None
        return data if data.get("proxy") else None

    def _write_bundle(self, proxy: str, ttl_seconds: int, profile_name: str) -> None:
        payload = json.dumps(
            {
                "proxy": proxy,
                "profile": profile_name,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ttl": ttl_seconds,
            },
            ensure_ascii=False,
        )
        self.redis.set(self.current_key, payload, ex=ttl_seconds)

    # ---------------- 对外接口 ----------------
    async def get_bundle(self, force_new: bool = False) -> Tuple[str, Dict]:
        """返回 (proxy_url, 设备画像)。proxy_url 可直接给 httpx 用。"""
        if force_new:
            self.mark_invalid("主动轮换")
        bundle = self._read_bundle()
        if bundle is None:
            await self._refresh()
            bundle = self._read_bundle()
            if bundle is None:
                raise ProxyUnavailable("提取代理后仍未读到有效记录")
        profile = pick_profile(bundle.get("profile"), self.fingerprint_platform)
        return self.to_proxy_url(bundle["proxy"]), profile

    def to_proxy_url(self, proxy: str) -> str:
        if self.username and self.password:
            return f"http://{self.username}:{self.password}@{proxy}/"
        # 白名单方式（需提前在快代理后台设置白名单）
        return f"http://{proxy}/"

    def mark_invalid(self, reason: str = "") -> None:
        """标记当前 IP 不可用并删除，下次取用会自动换新 IP。"""
        bundle = self._read_bundle()
        if bundle:
            self.redis.delete(self.current_key)
            logger.info("[代理:%s] 丢弃 IP %s（原因：%s）", self.scope, bundle["proxy"], reason or "未知")

    def remaining_ttl(self) -> int:
        try:
            return self.redis.ttl(self.current_key)
        except Exception:  # noqa: BLE001
            return -1

    def current_ip(self) -> Optional[str]:
        bundle = self._read_bundle()
        return bundle["proxy"] if bundle else None

    # ---------------- 提取 ----------------
    async def _refresh(self) -> str:
        """带分布式锁的提取：并发场景下只有一个进程真正调提取接口。"""
        async with self._async_lock:
            bundle = self._read_bundle()
            if bundle:
                return bundle["proxy"]

            deadline = time.time() + 60
            while time.time() < deadline:
                if self.redis.set(self.lock_key, "1", ex=20, nx=True):
                    try:
                        bundle = self._read_bundle()  # 双检，可能已被别的进程写好
                        if bundle:
                            return bundle["proxy"]
                        proxy, ttl = await self._fetch_valid_proxy()
                        profile = pick_profile(platform=self.fingerprint_platform)
                        self._write_bundle(proxy, ttl, profile["name"])
                        logger.info(
                            "[代理:%s] 启用新 IP %s，指纹 %s，有效期约 %d 秒",
                            self.scope, proxy, profile["name"], ttl,
                        )
                        return proxy
                    finally:
                        self.redis.delete(self.lock_key)

                await asyncio.sleep(0.5)
                bundle = self._read_bundle()
                if bundle:
                    return bundle["proxy"]

            raise ProxyUnavailable("等待其他进程提取代理 IP 超时（60s）")

    async def _fetch_valid_proxy(self) -> Tuple[str, int]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.fetch_retries + 1):
            try:
                candidates = await self._call_api(num=1)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                candidates = []

            for proxy, api_ttl in candidates:
                ttl = self._resolve_ttl(api_ttl)
                if not self.validate_on_fetch or await self._validate(proxy):
                    return proxy, ttl
                last_error = RuntimeError(f"IP {proxy} 连通性校验失败")

            await asyncio.sleep(min(2 * attempt, 8))

        raise ProxyUnavailable(
            f"提取可用代理 IP 失败（重试 {self.fetch_retries} 次）：{last_error}"
        )

    def _resolve_ttl(self, api_ttl: Optional[float]) -> int:
        """以快代理返回的剩余时长为准，并限制在 [min_ttl, max_ttl] 区间内。"""
        if api_ttl:
            ttl = min(int(api_ttl), self.max_ttl_seconds)
            if ttl < self.min_ttl_seconds:
                logger.info(
                    "[代理:%s] 本次 IP 剩余时长仅 %d 秒，低于设定下限 %d 秒（受订单套餐限制）",
                    self.scope, ttl, self.min_ttl_seconds,
                )
        else:
            ttl = random.randint(self.min_ttl_seconds, self.max_ttl_seconds)
        return max(60, ttl - self.safety_buffer_seconds)

    # ---------------- 鉴权 ----------------
    async def _signature(self, force_new: bool = False) -> str:
        """plain：直接用 SecretKey 明文；token：换 secret_token 并缓存到 Redis。"""
        if self.auth_mode == "plain":
            return self.secret_key

        if force_new:
            self.redis.delete(self.token_key)
        else:
            cached = self.redis.get(self.token_key)
            if cached:
                return cached

        token, ttl = await self._fetch_secret_token()
        self.redis.set(self.token_key, token, ex=ttl)
        logger.info("[代理] 已获取新的 secret_token，缓存 %d 秒", ttl)
        return token

    async def _fetch_secret_token(self) -> Tuple[str, int]:
        async with httpx.AsyncClient(timeout=self.api_timeout) as client:
            resp = await client.post(
                self.AUTH_TOKEN_URL,
                data={"secret_id": self.secret_id, "secret_key": self.secret_key},
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"令牌接口返回非 JSON：{resp.text[:200]}") from exc

        if data.get("code") != 0:
            raise RuntimeError(
                f"获取 secret_token 失败：code={data.get('code')}，msg={data.get('msg')}"
            )
        payload = data.get("data") or {}
        token = payload.get("secret_token")
        if not token:
            raise RuntimeError(f"令牌接口未返回 secret_token：{data}")
        # 官方令牌有效期约 1 小时，提前 2 分钟过期以免临界失败
        expire = int(float(payload.get("expire") or 3600))
        return token, max(60, expire - 120)

    # ---------------- 提取接口 ----------------
    async def _call_api(self, num: int = 1) -> List[Tuple[str, Optional[float]]]:
        try:
            return await self._request_proxies(num, await self._signature())
        except AuthRejected:
            if self.auth_mode == "plain":
                raise
            logger.warning("[代理] secret_token 被拒绝，重新获取后重试")
            return await self._request_proxies(num, await self._signature(force_new=True))

    async def _request_proxies(self, num: int, signature: str) -> List[Tuple[str, Optional[float]]]:
        params = {
            "secret_id": self.secret_id,
            "signature": signature,
            "num": num,
            "pt": 1,        # 1=HTTP/HTTPS 代理
            "format": "json",
            "sep": 1,
            "f_et": 1,      # 返回 IP 剩余时长（秒）
        }
        url = f"{self.API_URL}?{urlencode(params)}"
        async with httpx.AsyncClient(timeout=self.api_timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text.strip()
            try:
                data = resp.json()
            except ValueError:
                # 部分套餐只回文本，按行解析兜底
                result = self._parse_proxy_lines(text.splitlines())
                if result:
                    return result
                raise RuntimeError(f"提取接口返回无法解析：{text[:200]}")

        if data.get("code") != 0:
            message = f"提取接口报错：code={data.get('code')}，msg={data.get('msg')}"
            if self._is_auth_error(data):
                raise AuthRejected(message)
            raise RuntimeError(message)

        result = self._parse_proxy_lines((data.get("data") or {}).get("proxy_list") or [])
        if not result:
            raise RuntimeError(f"提取接口未返回可用 IP：{data}")
        return result

    @staticmethod
    def _parse_proxy_lines(lines) -> List[Tuple[str, Optional[float]]]:
        result: List[Tuple[str, Optional[float]]] = []
        for line in lines:
            match = PROXY_LINE_RE.match(str(line))
            if match:
                result.append((match.group(1), float(match.group(2)) if match.group(2) else None))
        return result

    @staticmethod
    def _is_auth_error(data: Dict) -> bool:
        """判断是不是签名/令牌类错误，用于决定要不要重新换 token。"""
        text = f"{data.get('code')} {data.get('msg', '')}".lower()
        keywords = ("signature", "token", "sign", "auth", "secret", "签名", "令牌", "鉴权")
        return any(word in text for word in keywords)

    async def _validate(self, proxy: str) -> bool:
        endpoint = self.to_proxy_url(proxy)
        try:
            async with httpx.AsyncClient(proxy=endpoint, timeout=self.api_timeout) as client:
                resp = await client.get(self.TEST_URL)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
