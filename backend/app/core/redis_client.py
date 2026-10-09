"""Redis：代理 IP 缓存、采集水印、去重位、分布式锁。

Redis 关闭或连不上时自动退化为进程内实现，功能不中断，
只是多进程/多机之间不再共享状态——日志里会明确提示。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional, Set
from urllib.parse import quote

from .config import Config
from .logging import get_logger

logger = get_logger(__name__)


def build_redis_url(cfg: dict) -> str:
    scheme = "rediss" if cfg.get("ssl") else "redis"
    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 6379)}/{cfg.get('db', 0)}"


def mask_redis_url(url: str) -> str:
    import re
    return re.sub(r"://([^:/@]*):([^@]*)@", lambda m: f"://{m.group(1)}:***@", url)


class MemoryStore:
    """Redis 不可用时的本地退化实现（仅当前进程有效）。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, Optional[float]]] = {}
        self._sets: dict[str, Set[str]] = {}
        self._lock = threading.Lock()

    def _expired(self, key: str) -> bool:
        item = self._data.get(key)
        if not item:
            return True
        _, expire_at = item
        if expire_at and expire_at <= time.time():
            self._data.pop(key, None)
            return True
        return False

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            if self._expired(key):
                return None
            return self._data[key][0]

    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False) -> Optional[bool]:
        with self._lock:
            if nx and not self._expired(key):
                return None
            self._data[key] = (value, time.time() + ex if ex else None)
            return True

    def delete(self, *keys: str) -> int:
        with self._lock:
            return sum(1 for k in keys if self._data.pop(k, None) is not None)

    def ttl(self, key: str) -> int:
        with self._lock:
            item = self._data.get(key)
            if not item or not item[1]:
                return -1
            return int(item[1] - time.time())

    def exists(self, key: str) -> int:
        with self._lock:
            return 0 if self._expired(key) else 1

    def sadd(self, key: str, *values: str) -> int:
        with self._lock:
            bucket = self._sets.setdefault(key, set())
            before = len(bucket)
            bucket.update(values)
            return len(bucket) - before

    def sismember(self, key: str, value: str) -> bool:
        with self._lock:
            return value in self._sets.get(key, set())

    def expire(self, key: str, seconds: int) -> bool:
        with self._lock:
            item = self._data.get(key)
            if item:
                self._data[key] = (item[0], time.time() + seconds)
                return True
            return False

    def ping(self) -> bool:
        return True


class RedisClient:
    """薄封装：对外只暴露本项目用到的操作，内部决定走 Redis 还是内存。"""

    def __init__(self, config: Config):
        cfg = config.get("redis", {}) or {}
        self.prefix = cfg.get("key_prefix", "smc")
        self.enabled = bool(cfg.get("enabled"))
        self.watermark_ttl = int(cfg.get("watermark_ttl_days", 90)) * 86400
        self.dedupe_ttl = int(cfg.get("dedupe_ttl_days", 30)) * 86400
        self.backend: Any
        self.is_real_redis = False

        if not self.enabled:
            logger.warning("Redis 未启用，代理 IP 与去重状态仅保存在当前进程内存中")
            self.backend = MemoryStore()
            return

        url = build_redis_url(cfg)
        try:
            import redis as redis_lib

            kwargs = {
                "decode_responses": True,
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
                "health_check_interval": 30,
            }
            try:
                # protocol=2 强制 RESP2：Redis 6.0 以下不认 HELLO 命令
                client = redis_lib.Redis.from_url(url, protocol=int(cfg.get("protocol", 2)), **kwargs)
            except TypeError:
                client = redis_lib.Redis.from_url(url, **kwargs)
            client.ping()
            self.backend = client
            self.is_real_redis = True
            logger.info("Redis 已连接：%s（key 前缀 %s）", mask_redis_url(url), self.prefix)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Redis 连接失败（%s）：%s —— 已退化为进程内缓存，"
                "多进程部署时代理与去重状态将不共享", mask_redis_url(url), exc,
            )
            self.backend = MemoryStore()

    # ---------------- key 拼装 ----------------
    def key(self, *parts: str) -> str:
        return ":".join([self.prefix, *[str(p) for p in parts]])

    # ---------------- 通用 ----------------
    def get(self, key: str) -> Optional[str]:
        return self.backend.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False) -> Optional[bool]:
        return self.backend.set(key, value, ex=ex, nx=nx)

    def delete(self, *keys: str) -> int:
        return self.backend.delete(*keys)

    def ttl(self, key: str) -> int:
        return self.backend.ttl(key)

    def ping(self) -> bool:
        try:
            return bool(self.backend.ping())
        except Exception:  # noqa: BLE001
            return False

    # ---------------- 采集水印 ----------------
    def get_watermark(self, scenic_id: str, channel: str, scope: str) -> Optional[str]:
        """scope 例如 keyword:西湖 或 poi:32289，返回上次采到的位置标记。"""
        return self.get(self.key("wm", scenic_id, channel, scope))

    def set_watermark(self, scenic_id: str, channel: str, scope: str, value: str) -> None:
        self.set(self.key("wm", scenic_id, channel, scope), value, ex=self.watermark_ttl)

    # ---------------- 去重 ----------------
    def seen(self, channel: str, kind: str, item_id: str) -> bool:
        """kind: work | comment。返回 True 表示本轮之前已处理过。"""
        return self.backend.exists(self.key("seen", channel, kind, item_id)) == 1

    def mark_seen(self, channel: str, kind: str, item_id: str) -> None:
        self.set(self.key("seen", channel, kind, item_id), "1", ex=self.dedupe_ttl)


_client: Optional[RedisClient] = None


def init_redis(config: Config) -> RedisClient:
    global _client
    _client = RedisClient(config)
    return _client


def get_redis() -> RedisClient:
    if _client is None:
        raise RuntimeError("Redis 尚未初始化，请先调用 init_redis(config)")
    return _client
