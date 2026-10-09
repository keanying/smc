"""失败池：模型标注失败的评论存这里，等着后续重标。

为什么要单独一个存储，而不是复用队列的死信：

    队列后端可能是 memory（进程内），进程一重启失败记录就没了，
    "后续再标注"就无从谈起。失败池固定走 Redis，与队列后端解耦——
    哪怕队列用 memory，失败记录也是持久的。

存什么：**整条评论的原始字段**，不只是主键。
这样重标时直接从失败池取出来就能提交，不用回 MySQL 再查一遍
（那些行的 sentiment_label 还是 NULL，回查是能查到，但多一次全表条件扫描不划算）。

Redis 结构：

    HASH  {prefix}:failed     field = 业务主键  →  value = 记录 JSON
    ZSET  {prefix}:failed:z   member = 业务主键 →  score = 最后失败时间戳

HASH 存内容、ZSET 排时间。用 ZSET 是为了三件事：按最早失败的先重试、
按时间窗口挑（"只重试 10 分钟前失败的，刚失败的可能是模型还在抽风"）、
以及按保留期清理过期记录。
"""

from __future__ import annotations

import abc
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..config import FailureStoreConfig
from ..domain.models import CommentRecord
from ..logging_conf import get_logger
from ..redis_compat import create_redis_client, explain_redis_error

__all__ = [
    "FailureRecord",
    "BaseFailureStore",
    "RedisFailureStore",
    "MemoryFailureStore",
    "NullFailureStore",
    "create_failure_store",
]

logger = get_logger(__name__)


@dataclass
class FailureRecord:
    """一条标注失败的记录。"""

    record: Dict[str, Any]           # CommentRecord 的完整字段，重标时直接用
    error: str = ""                  # 最后一次失败原因
    attempts: int = 0                # 累计失败次数（跨进程累加）
    first_failed_at: float = field(default_factory=time.time)
    last_failed_at: float = field(default_factory=time.time)

    @property
    def comment_id(self) -> str:
        return str(self.record.get("comment_id", ""))

    def key(self) -> str:
        """业务主键，用作 Redis 的 field/member。

        用 channel|scenic_id|comment_id 而不是只用 comment_id：
        与写回的 key_columns 保持一致，避免跨渠道撞号。
        """
        r = self.record
        return f"{r.get('channel', '')}|{r.get('scenic_id', '')}|{r.get('comment_id', '')}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "FailureRecord":
        data = json.loads(payload)
        return cls(
            record=data.get("record") or {},
            error=str(data.get("error") or ""),
            attempts=int(data.get("attempts") or 0),
            first_failed_at=float(data.get("first_failed_at") or time.time()),
            last_failed_at=float(data.get("last_failed_at") or time.time()),
        )

    def to_comment(self) -> CommentRecord:
        return CommentRecord.from_dict(self.record)

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.last_failed_at)


# ---------------------------------------------------------------------------
class BaseFailureStore(abc.ABC):
    """失败池接口。实现类必须线程安全。"""

    @abc.abstractmethod
    def record(self, record: CommentRecord, error: str) -> None:
        """记一条失败。同一条评论重复失败时累加 attempts、刷新时间，不产生重复记录。"""

    @abc.abstractmethod
    def take(self, limit: int, *, min_age_seconds: float = 0.0) -> List[FailureRecord]:
        """取出一批待重标记录并**从池中移除**。

        取出即移除是刻意的：重标如果又失败，会由 :meth:`record` 重新放回去
        （attempts 继续累加）；这样多个重标进程同时跑也不会把同一条领两次。

        Args:
            limit: 最多取多少条。
            min_age_seconds: 只取失败时间早于这个秒数的记录。刚失败的先晾一会儿，
                避免模型正在抽风时反复重试同一批。
        """

    @abc.abstractmethod
    def peek(self, limit: int = 20) -> List[FailureRecord]:
        """看一眼池子里有什么，不移除。排查用。"""

    @abc.abstractmethod
    def count(self) -> int:
        """池子里有多少条。"""

    def prune(self) -> int:
        """清理超过保留期的记录，返回清掉的条数。默认不做事。"""
        return 0

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
class NullFailureStore(BaseFailureStore):
    """不记录任何东西。``failure_store.enabled: false`` 时用。

    注意：关掉失败池意味着模型失败的评论**彻底丢了**——既不写库，也没人记得它，
    只在日志里留一行。所以 :meth:`record` 这里要打 WARNING。
    """

    def record(self, record: CommentRecord, error: str) -> None:
        logger.warning("失败池已关闭，评论 %s 的失败不会被记录，无法后续重标：%s",
                       record.comment_id, error)

    def take(self, limit: int, *, min_age_seconds: float = 0.0) -> List[FailureRecord]:
        return []

    def peek(self, limit: int = 20) -> List[FailureRecord]:
        return []

    def count(self) -> int:
        return 0


# ---------------------------------------------------------------------------
class MemoryFailureStore(BaseFailureStore):
    """进程内失败池。仅供单测与无 Redis 的临时环境使用。"""

    def __init__(self, cfg: FailureStoreConfig) -> None:
        self._cfg = cfg
        self._items: Dict[str, FailureRecord] = {}
        self._lock = threading.Lock()

    def record(self, record: CommentRecord, error: str) -> None:
        item = FailureRecord(record=record.to_dict(), error=error[:500],
                             attempts=int(getattr(record, "retry_count", 0)) + 1)
        with self._lock:
            existing = self._items.get(item.key())
            if existing:
                existing.attempts = max(existing.attempts + 1, item.attempts)
                existing.error = item.error
                existing.last_failed_at = time.time()
            else:
                self._items[item.key()] = item
                self._enforce_capacity()

    def _enforce_capacity(self) -> None:
        if not self._cfg.max_records or len(self._items) <= self._cfg.max_records:
            return
        overflow = len(self._items) - self._cfg.max_records
        oldest = sorted(self._items.items(), key=lambda kv: kv[1].last_failed_at)[:overflow]
        for key, _ in oldest:
            self._items.pop(key, None)

    def take(self, limit: int, *, min_age_seconds: float = 0.0) -> List[FailureRecord]:
        deadline = time.time() - min_age_seconds
        with self._lock:
            eligible = sorted(
                (i for i in self._items.values() if i.last_failed_at <= deadline),
                key=lambda i: i.last_failed_at,
            )[:limit]
            for item in eligible:
                self._items.pop(item.key(), None)
        return eligible

    def peek(self, limit: int = 20) -> List[FailureRecord]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: i.last_failed_at)[:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def prune(self) -> int:
        if not self._cfg.ttl_days:
            return 0
        deadline = time.time() - self._cfg.ttl_days * 86400
        with self._lock:
            expired = [k for k, v in self._items.items() if v.last_failed_at < deadline]
            for key in expired:
                self._items.pop(key, None)
        return len(expired)


# ---------------------------------------------------------------------------
class RedisFailureStore(BaseFailureStore):
    """Redis 失败池（生产用）。线程安全。"""

    def __init__(self, cfg: FailureStoreConfig, *, client=None) -> None:
        self._cfg = cfg
        self._r = client if client is not None else create_redis_client(cfg.redis)
        self.last_error = ""
        prefix = cfg.redis.key_prefix.rstrip(":")
        self.k_hash = f"{prefix}:failed"
        self.k_zset = f"{prefix}:failed:z"

    # ------------------------------------------------------------------ 写
    def record(self, record: CommentRecord, error: str) -> None:
        # retry_count 是这条评论已经历过的重标轮次（随记录在队列里流转），
        # take() 会把记录从池子里删掉，所以累计次数只能靠它跨轮次带过来
        item = FailureRecord(record=record.to_dict(), error=error[:500],
                             attempts=int(getattr(record, "retry_count", 0)) + 1)
        key = item.key()

        # 同一条评论重复失败：累加次数、保留首次失败时间，不新增记录
        existing_raw = self._r.hget(self.k_hash, key)
        if existing_raw:
            try:
                existing = FailureRecord.from_json(existing_raw)
                item.attempts = max(existing.attempts + 1, item.attempts)
                item.first_failed_at = existing.first_failed_at
            except (ValueError, KeyError):
                pass    # 旧记录坏了就当新的写

        pipe = self._r.pipeline()
        pipe.hset(self.k_hash, key, item.to_json())
        pipe.zadd(self.k_zset, {key: item.last_failed_at})
        pipe.execute()
        self._enforce_capacity()

    def _enforce_capacity(self) -> None:
        """池子写爆会把 Redis 内存吃干净，超了就从最老的开始丢。"""
        if not self._cfg.max_records:
            return
        size = int(self._r.zcard(self.k_zset) or 0)
        if size <= self._cfg.max_records:
            return
        overflow = size - self._cfg.max_records
        victims = self._r.zrange(self.k_zset, 0, overflow - 1) or []
        if victims:
            pipe = self._r.pipeline()
            pipe.zrem(self.k_zset, *victims)
            pipe.hdel(self.k_hash, *victims)
            pipe.execute()
            logger.warning("失败池超过上限 %d，丢弃最早的 %d 条记录",
                           self._cfg.max_records, len(victims))

    # ------------------------------------------------------------------ 读
    def take(self, limit: int, *, min_age_seconds: float = 0.0) -> List[FailureRecord]:
        deadline = time.time() - max(0.0, min_age_seconds)
        keys = self._r.zrangebyscore(self.k_zset, "-inf", deadline, start=0, num=limit)
        if not keys:
            return []

        payloads = self._r.hmget(self.k_hash, keys)
        pipe = self._r.pipeline()
        pipe.zrem(self.k_zset, *keys)
        pipe.hdel(self.k_hash, *keys)
        pipe.execute()

        out: List[FailureRecord] = []
        for key, payload in zip(keys, payloads):
            if not payload:
                continue
            try:
                out.append(FailureRecord.from_json(payload))
            except (ValueError, KeyError) as exc:
                logger.error("失败池记录损坏，已丢弃：key=%s err=%s", key, exc)
        return out

    def peek(self, limit: int = 20) -> List[FailureRecord]:
        keys = self._r.zrange(self.k_zset, 0, max(0, limit - 1)) or []
        if not keys:
            return []
        out: List[FailureRecord] = []
        for payload in self._r.hmget(self.k_hash, keys):
            if not payload:
                continue
            try:
                out.append(FailureRecord.from_json(payload))
            except (ValueError, KeyError):
                continue
        return out

    def count(self) -> int:
        return int(self._r.zcard(self.k_zset) or 0)

    def prune(self) -> int:
        """清掉超过保留期的记录。``ttl_days=0`` 表示永久保留。"""
        if not self._cfg.ttl_days:
            return 0
        deadline = time.time() - self._cfg.ttl_days * 86400
        keys = self._r.zrangebyscore(self.k_zset, "-inf", deadline) or []
        if not keys:
            return 0
        pipe = self._r.pipeline()
        pipe.zrem(self.k_zset, *keys)
        pipe.hdel(self.k_hash, *keys)
        pipe.execute()
        logger.info("失败池清理过期记录 %d 条（保留期 %d 天）", len(keys), self._cfg.ttl_days)
        return len(keys)

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception as exc:            # noqa: BLE001
            self.last_error = explain_redis_error(exc, self._cfg.redis)
            logger.error("失败池 Redis 连接失败：%s", self.last_error)
            return False

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:                   # noqa: BLE001  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
def create_failure_store(cfg: FailureStoreConfig, *, client=None) -> BaseFailureStore:
    """按配置创建失败池。

    Raises:
        ValueError: 未知后端。
        RuntimeError: 选了 redis 但连不上——这必须在启动期炸，
            否则跑到一半才发现失败记录全丢了。
    """
    if not cfg.enabled or cfg.backend == "none":
        return NullFailureStore()
    if cfg.backend == "memory":
        return MemoryFailureStore(cfg)
    if cfg.backend == "redis":
        store = RedisFailureStore(cfg, client=client)
        if client is None and not store.ping():
            raise RuntimeError(f"失败池连不上 Redis —— {store.last_error}")
        return store
    raise ValueError(f"未知失败池后端：{cfg.backend!r}")
