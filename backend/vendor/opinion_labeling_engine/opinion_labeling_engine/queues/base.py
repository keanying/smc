"""队列抽象。

设计约束（来自需求）：
    - 数据是「一边采集一边推进来」的流式输入，所以队列必须支持随时入队。
    - 不依赖业务表做队列：源表只在最后一步被 UPDATE，不承担任务状态职责。

投递语义为 **at-least-once**：消费者拿到租约（Lease）后必须显式 ack；
未 ack 的任务在可见性超时后会被回收重投。因为写回是按主键 UPDATE 的幂等操作，
重复消费只多花一次模型调用，不会产生脏数据。
"""

from __future__ import annotations

import abc
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..domain.models import CommentRecord

__all__ = ["QueueTask", "Lease", "BaseQueue", "QueueFullError"]


class QueueFullError(RuntimeError):
    """队列已满且策略为 drop 时抛出。"""


@dataclass
class QueueTask:
    """队列里流转的一条任务。"""

    record: CommentRecord
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.time)
    last_error: str = ""

    # -------------------------------------------------------------- 序列化
    def to_json(self) -> str:
        return json.dumps(
            {
                "msg_id": self.msg_id,
                "attempts": self.attempts,
                "enqueued_at": self.enqueued_at,
                "last_error": self.last_error,
                "record": self.record.to_dict(),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "QueueTask":
        data = json.loads(payload)
        return cls(
            record=CommentRecord.from_dict(data["record"]),
            msg_id=data.get("msg_id") or uuid.uuid4().hex,
            attempts=int(data.get("attempts") or 0),
            enqueued_at=float(data.get("enqueued_at") or time.time()),
            last_error=str(data.get("last_error") or ""),
        )

    def wait_seconds(self) -> float:
        """入队到现在的排队时长，用于观测积压。"""
        return max(0.0, time.time() - self.enqueued_at)


@dataclass
class Lease:
    """一次消费的租约。消费完必须 ack 或 nack，否则会被回收重投。"""

    task: QueueTask
    #: 后端私有句柄（Redis 用原始 payload 串做 LREM）
    handle: Any = None


class BaseQueue(abc.ABC):
    """队列后端接口。实现类必须线程安全。"""

    @abc.abstractmethod
    def put(self, task: QueueTask) -> bool:
        """入队。

        Returns:
            True 表示入队成功；队列满且策略为 drop 时返回 False。
        """

    @abc.abstractmethod
    def get(self, timeout: Optional[float] = None) -> Optional[Lease]:
        """取一条任务并进入 in-flight 状态。超时返回 None。"""

    @abc.abstractmethod
    def ack(self, lease: Lease) -> None:
        """确认处理完成，从 in-flight 中移除。"""

    @abc.abstractmethod
    def nack(self, lease: Lease, *, error: str = "", requeue: bool = True) -> bool:
        """处理失败。``requeue=True`` 且未超过最大重试次数时重新入队，否则进死信。

        Returns:
            True 表示这条已经放弃重试、进了死信（调用方该把它记进失败池）；
            False 表示还会再试一次。
        """

    @abc.abstractmethod
    def size(self) -> int:
        """待处理任务数（不含 in-flight）。"""

    @abc.abstractmethod
    def inflight(self) -> int:
        """已取出但未 ack 的任务数。"""

    @abc.abstractmethod
    def dead_size(self) -> int:
        """死信数量。"""

    def stats(self) -> Dict[str, int]:
        return {"pending": self.size(), "inflight": self.inflight(), "dead": self.dead_size()}

    def reclaim(self) -> int:
        """回收超时未 ack 的任务，返回回收条数。默认不做任何事。"""
        return 0

    def close(self) -> None:
        """释放连接等资源。"""

    # -------------------------------------------------------------- 便捷方法
    def submit(self, record: CommentRecord) -> bool:
        """把一条评论包成任务入队。"""
        return self.put(QueueTask(record=record))
