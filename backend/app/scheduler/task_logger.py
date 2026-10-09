"""任务日志：一份进 MySQL（可回看），一份推给 WebSocket（实时终端）。

写库是攒批的——采集时日志很密，一条一次 INSERT 会明显拖慢采集。
默认攒满 50 条或超过 2 秒就落一次盘。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Set

from ..core.logging import get_logger
from ..repositories.task_repo import TaskRepository

logger = get_logger(__name__)

FLUSH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 2.0
#: 每个任务在内存里保留的最近日志条数，供前端打开页面时补齐历史
RECENT_BUFFER = 500


class LogHub:
    """极简发布订阅：WebSocket 连接订阅某个 task_id，采集侧往里推。"""

    def __init__(self) -> None:
        self._subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
        self._recent: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=RECENT_BUFFER)
        )

    def subscribe(self, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(task_id, set()).discard(queue)
        if not self._subscribers.get(task_id):
            self._subscribers.pop(task_id, None)

    def recent(self, task_id: str) -> List[Dict[str, Any]]:
        return list(self._recent.get(task_id, []))

    def publish(self, task_id: str, record: Dict[str, Any]) -> None:
        self._recent[task_id].append(record)
        for queue in list(self._subscribers.get(task_id, set())):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # 前端跟不上就丢最旧的，宁可丢日志也不能阻塞采集
                try:
                    queue.get_nowait()
                    queue.put_nowait(record)
                except Exception:  # noqa: BLE001
                    pass

    def clear(self, task_id: str) -> None:
        self._recent.pop(task_id, None)


LOG_HUB = LogHub()


class TaskLogger:
    """采集器拿到的就是这个对象，用法和普通 logger 一样。"""

    def __init__(self, task_id: str, repo: TaskRepository, channel: str = ""):
        self.task_id = task_id
        self.repo = repo
        self.channel = channel
        self._buffer: List[Dict[str, Any]] = []
        self._last_flush = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    def bind(self, channel: str) -> "TaskLogger":
        """给某个平台的采集过程打上 channel 标签，日志里能分辨来源。"""
        child = TaskLogger(self.task_id, self.repo, channel)
        child._buffer = self._buffer          # 共用缓冲，统一落盘
        child._lock = self._lock
        return child

    def _record(self, level: str, message: str) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "log_time": datetime.now(),
            "level": level,
            "channel": self.channel or None,
            "message": str(message),
        }

    def _emit(self, level: str, message: str) -> None:
        record = self._record(level, message)
        self._buffer.append(record)
        LOG_HUB.publish(self.task_id, {
            **record, "log_time": record["log_time"].strftime("%Y-%m-%d %H:%M:%S"),
        })
        getattr(logger, level.lower(), logger.info)("[task %s] %s", self.task_id, message)
        if len(self._buffer) >= FLUSH_SIZE:
            asyncio.create_task(self.flush())

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    warning = warn

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def debug(self, message: str) -> None:
        self._emit("DEBUG", message)

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            pending = list(self._buffer)
            self._buffer.clear()
        try:
            await self.repo.add_logs(pending)
        except Exception as exc:  # noqa: BLE001
            logger.error("任务日志落库失败：%s", exc)
