# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

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
