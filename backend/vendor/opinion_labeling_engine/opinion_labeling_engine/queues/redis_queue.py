"""Redis 队列（生产环境默认）。

可靠投递做法：
    入队    LPUSH  {p}:pending
    出队    BRPOPLPUSH {p}:pending -> {p}:processing   （原子搬运，进程崩了任务还在）
            同时 HSET {p}:inflight <msg_id> <租约时间戳>
    确认    LREM {p}:processing 1 <payload> + HDEL {p}:inflight <msg_id>
    失败    从 processing 摘除后按 attempts 决定重入 pending 还是进 {p}:dead
    回收    扫描 processing，租约时间戳超过 visibility_timeout 的搬回 pending

这样即使 worker 进程被 kill -9，任务也只是停在 processing 里，
下一轮 reclaim 会把它捞回来，不会丢。

多进程 / 多机部署时，所有实例连同一个 Redis、用同一个 key_prefix 即可水平扩展。
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from ..config import QueueConfig
from ..logging_conf import get_logger
from ..redis_compat import create_redis_client, explain_redis_error
from .base import BaseQueue, Lease, QueueTask

__all__ = ["RedisQueue"]

logger = get_logger(__name__)


class RedisQueue(BaseQueue):
    """Redis List 实现的可靠队列。线程安全（redis-py 连接池本身线程安全）。"""

    def __init__(self, cfg: QueueConfig, *, client=None) -> None:
        self._cfg = cfg
        rcfg = cfg.redis
        self._r = client if client is not None else create_redis_client(rcfg)
        self.last_error = ""

        p = rcfg.key_prefix.rstrip(":")
        self.k_pending = f"{p}:pending"
        self.k_processing = f"{p}:processing"
        self.k_inflight = f"{p}:inflight"
        self.k_dead = f"{p}:dead"

        self._last_reclaim = 0.0
        self._reclaim_lock = threading.Lock()

    # ------------------------------------------------------------------ 入队
    def put(self, task: QueueTask) -> bool:
        payload = task.to_json()
        if self._cfg.max_size and self._r.llen(self.k_pending) >= self._cfg.max_size:
            if self._cfg.full_policy == "drop":
                logger.warning("Redis 队列已满（%d），丢弃任务 %s",
                               self._cfg.max_size, task.msg_id)
                return False
            # block 策略：轮询等待，间隔刻意放大，避免打爆 Redis
            while self._r.llen(self.k_pending) >= self._cfg.max_size:
                time.sleep(0.2)
        self._r.lpush(self.k_pending, payload)
        return True

    # ------------------------------------------------------------------ 出队
    def get(self, timeout: Optional[float] = None) -> Optional[Lease]:
        wait = self._cfg.pop_timeout if timeout is None else timeout
        # BRPOPLPUSH 的 timeout 只接受整数秒，0 表示永久阻塞——这里至少给 1 秒，
        # 否则 worker 停机时会卡在无限阻塞上退不出来。
        block = max(1, int(round(wait)))
        payload = self._r.brpoplpush(self.k_pending, self.k_processing, timeout=block)
        if payload is None:
            self._maybe_reclaim()
            return None

        try:
            task = QueueTask.from_json(payload)
        except (ValueError, KeyError) as exc:
            # 脏消息：直接扔死信，不能让它反复卡住消费
            logger.error("队列消息无法反序列化，投入死信：%s；payload=%s", exc, payload[:300])
            self._r.lrem(self.k_processing, 1, payload)
            self._r.lpush(self.k_dead, payload)
            return None

        self._r.hset(self.k_inflight, task.msg_id, str(time.time()))
        return Lease(task=task, handle=payload)

    # ------------------------------------------------------------------ 确认
    def ack(self, lease: Lease) -> None:
        pipe = self._r.pipeline()
        pipe.lrem(self.k_processing, 1, lease.handle)
        pipe.hdel(self.k_inflight, lease.task.msg_id)
        pipe.execute()

    def nack(self, lease: Lease, *, error: str = "", requeue: bool = True) -> bool:
        task = lease.task
        task.attempts += 1
        task.last_error = error[:500]

        pipe = self._r.pipeline()
        pipe.lrem(self.k_processing, 1, lease.handle)
        pipe.hdel(self.k_inflight, task.msg_id)

        dead = not (requeue and task.attempts < self._cfg.max_attempts)
        if dead:
            logger.error("任务 %s 重试 %d 次仍失败，进入死信：%s",
                         task.msg_id, task.attempts, error)
            pipe.lpush(self.k_dead, task.to_json())
        else:
            logger.warning("任务 %s 第 %d 次失败，重新入队：%s",
                           task.msg_id, task.attempts, error)
            pipe.lpush(self.k_pending, task.to_json())
        pipe.execute()
        return dead

    # ------------------------------------------------------------------ 回收
    def _maybe_reclaim(self) -> None:
        """按 ``reclaim_interval`` 节流调用 :meth:`reclaim`。"""
        now = time.time()
        if now - self._last_reclaim < self._cfg.redis.reclaim_interval:
            return
        if not self._reclaim_lock.acquire(blocking=False):
            return
        try:
            self._last_reclaim = now
            recovered = self.reclaim()
            if recovered:
                logger.warning("回收超时任务 %d 条", recovered)
        finally:
            self._reclaim_lock.release()

    def reclaim(self) -> int:
        """把 processing 里租约超时的任务搬回 pending。

        通过 ``HSETNX`` 之外的方式做并发保护：多个实例同时 reclaim 时，
        ``LREM`` 只有一个会真正删掉那条 payload（返回 1），
        只有删成功的那个实例才会 LPUSH 回去，因此不会重复投递。
        """
        deadline = time.time() - self._cfg.redis.visibility_timeout
        payloads: List[str] = self._r.lrange(self.k_processing, 0, -1) or []
        recovered = 0

        for payload in payloads:
            try:
                task = QueueTask.from_json(payload)
            except (ValueError, KeyError):
                if self._r.lrem(self.k_processing, 1, payload):
                    self._r.lpush(self.k_dead, payload)
                continue

            leased_at_raw = self._r.hget(self.k_inflight, task.msg_id)
            try:
                leased_at = float(leased_at_raw) if leased_at_raw else 0.0
            except (TypeError, ValueError):
                leased_at = 0.0

            if leased_at and leased_at > deadline:
                continue    # 还在租约期内，正常处理中

            if self._r.lrem(self.k_processing, 1, payload):
                self._r.hdel(self.k_inflight, task.msg_id)
                self._r.lpush(self.k_pending, payload)
                recovered += 1

        return recovered

    # ------------------------------------------------------------------ 观测
    def size(self) -> int:
        return int(self._r.llen(self.k_pending) or 0)

    def inflight(self) -> int:
        return int(self._r.llen(self.k_processing) or 0)

    def dead_size(self) -> int:
        return int(self._r.llen(self.k_dead) or 0)

    def drain_dead(self, limit: int = 1000) -> List[QueueTask]:
        """取出死信用于补跑；取出即从死信队列移除。"""
        out: List[QueueTask] = []
        for _ in range(limit):
            payload = self._r.rpop(self.k_dead)
            if payload is None:
                break
            try:
                out.append(QueueTask.from_json(payload))
            except (ValueError, KeyError):
                logger.error("死信消息损坏，已丢弃：%s", payload[:200])
        return out

    def ping(self) -> bool:
        """启动自检：连不上 Redis 要在启动期就暴露，而不是消费时才发现。"""
        try:
            return bool(self._r.ping())
        except Exception as exc:  # noqa: BLE001 - redis 异常类型随版本变化
            self.last_error = explain_redis_error(exc, self._cfg.redis)
            logger.error("队列 Redis 连接失败：%s", self.last_error)
            return False

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass
