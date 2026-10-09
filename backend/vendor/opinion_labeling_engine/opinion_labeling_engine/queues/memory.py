"""进程内内存队列。

适用场景：单机联调、单测、以及数据量不大且能接受进程重启丢任务的部署。
它实现了与 Redis 后端完全一致的租约语义（ack / nack / 可见性超时回收），
所以从 memory 切到 redis 不需要改任何业务代码。
"""

from __future__ import annotations

import queue as _stdqueue
import threading
import time
from typing import Dict, List, Optional

from ..config import QueueConfig
from ..logging_conf import get_logger
from .base import BaseQueue, Lease, QueueFullError, QueueTask

__all__ = ["MemoryQueue"]

logger = get_logger(__name__)


class MemoryQueue(BaseQueue):
    """基于 ``queue.Queue`` 的内存队列，带 in-flight 跟踪与死信。"""

    def __init__(self, cfg: QueueConfig) -> None:
        self._cfg = cfg
        self._q: _stdqueue.Queue = _stdqueue.Queue(maxsize=max(1, cfg.max_size))
        self._inflight: Dict[str, tuple] = {}     # msg_id -> (task, leased_at)
        self._dead: List[QueueTask] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 入队
    def put(self, task: QueueTask) -> bool:
        try:
            if self._cfg.full_policy == "block":
                self._q.put(task, block=True)
            else:
                self._q.put_nowait(task)
            return True
        except _stdqueue.Full:
            logger.warning("内存队列已满（%d），丢弃任务 %s", self._cfg.max_size, task.msg_id)
            return False

    # ------------------------------------------------------------------ 出队
    def get(self, timeout: Optional[float] = None) -> Optional[Lease]:
        wait = self._cfg.pop_timeout if timeout is None else timeout
        try:
            task = self._q.get(block=True, timeout=wait)
        except _stdqueue.Empty:
            return None
        with self._lock:
            self._inflight[task.msg_id] = (task, time.time())
        return Lease(task=task, handle=task.msg_id)

    # ------------------------------------------------------------------ 确认
    def ack(self, lease: Lease) -> None:
        with self._lock:
            self._inflight.pop(lease.task.msg_id, None)

    def nack(self, lease: Lease, *, error: str = "", requeue: bool = True) -> bool:
        task = lease.task
        with self._lock:
            self._inflight.pop(task.msg_id, None)
        task.attempts += 1
        task.last_error = error[:500]

        if requeue and task.attempts < self._cfg.max_attempts:
            logger.warning("任务 %s 第 %d 次失败，重新入队：%s",
                           task.msg_id, task.attempts, error)
            self.put(task)
            return False

        logger.error("任务 %s 重试 %d 次仍失败，进入死信：%s",
                     task.msg_id, task.attempts, error)
        with self._lock:
            self._dead.append(task)
        return True

    # ------------------------------------------------------------------ 回收
    def reclaim(self) -> int:
        """回收超过可见性超时仍未 ack 的任务（worker 崩溃场景）。"""
        deadline = time.time() - self._cfg.redis.visibility_timeout
        expired: List[QueueTask] = []
        with self._lock:
            for msg_id, (task, leased_at) in list(self._inflight.items()):
                if leased_at < deadline:
                    expired.append(task)
                    self._inflight.pop(msg_id, None)
        for task in expired:
            logger.warning("任务 %s 租约超时，回收重投", task.msg_id)
            self.put(task)
        return len(expired)

    # ------------------------------------------------------------------ 观测
    def size(self) -> int:
        return self._q.qsize()

    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)

    def dead_size(self) -> int:
        with self._lock:
            return len(self._dead)

    def dead_letters(self) -> List[QueueTask]:
        """取死信快照，供人工排查或补跑。"""
        with self._lock:
            return list(self._dead)

    def drain_dead(self) -> List[QueueTask]:
        """取出并清空死信。"""
        with self._lock:
            dead, self._dead = self._dead, []
        return dead
