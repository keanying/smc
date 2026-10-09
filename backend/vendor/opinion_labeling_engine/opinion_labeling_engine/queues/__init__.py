"""队列层：内存与 Redis 两种后端，语义一致，配置切换。"""

from ..config import QueueConfig
from .base import BaseQueue, Lease, QueueFullError, QueueTask
from .memory import MemoryQueue

__all__ = ["BaseQueue", "Lease", "QueueFullError", "QueueTask", "MemoryQueue", "create_queue"]


def create_queue(cfg: QueueConfig, *, client=None) -> BaseQueue:
    """按配置创建队列后端。

    Args:
        cfg: 队列配置。
        client: 仅测试用，注入一个 fake redis。

    Raises:
        ValueError: 未知后端。
        RuntimeError: 选了 redis 但连不上。
    """
    if cfg.backend == "memory":
        return MemoryQueue(cfg)
    if cfg.backend == "redis":
        from .redis_queue import RedisQueue

        queue = RedisQueue(cfg, client=client)
        if client is None and not queue.ping():
            raise RuntimeError(f"队列连不上 Redis —— {queue.last_error}")
        return queue
    raise ValueError(f"未知队列后端：{cfg.backend!r}")
