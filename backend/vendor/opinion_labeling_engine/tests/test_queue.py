"""队列单测：租约、重试、死信、超时回收。"""

from __future__ import annotations

import time

import pytest

from opinion_labeling_engine.config import QueueConfig, RedisConfig
from opinion_labeling_engine.queues.base import QueueTask
from opinion_labeling_engine.queues.memory import MemoryQueue

from conftest import make_record


@pytest.fixture()
def q() -> MemoryQueue:
    return MemoryQueue(QueueConfig(backend="memory", max_size=100, max_attempts=3, pop_timeout=0.1))


def test_put_get_ack(q):
    q.submit(make_record("c1", "风景不错"))
    assert q.size() == 1

    lease = q.get()
    assert lease is not None
    assert lease.task.record.comment_id == "c1"
    assert q.size() == 0
    assert q.inflight() == 1

    q.ack(lease)
    assert q.inflight() == 0
    assert q.dead_size() == 0


def test_get_on_empty_returns_none(q):
    assert q.get(timeout=0.05) is None


def test_nack_requeues_until_max_attempts(q):
    q.submit(make_record("c1", "x"))
    for expected_attempts in (1, 2):
        lease = q.get()
        q.nack(lease, error="boom")
        assert lease.task.attempts == expected_attempts
        assert q.size() == 1
        assert q.dead_size() == 0

    lease = q.get()
    q.nack(lease, error="boom")
    assert q.size() == 0
    assert q.dead_size() == 1


def test_dead_letter_keeps_payload(q):
    q.submit(make_record("c9", "内容"))
    for _ in range(3):
        lease = q.get()
        q.nack(lease, error="失败原因")
    dead = q.dead_letters()
    assert len(dead) == 1
    assert dead[0].record.comment_id == "c9"
    assert dead[0].last_error == "失败原因"


def test_reclaim_expired_lease():
    cfg = QueueConfig(backend="memory", pop_timeout=0.1,
                      redis=RedisConfig(visibility_timeout=0))
    q = MemoryQueue(cfg)
    q.submit(make_record("c1", "x"))
    lease = q.get()
    assert q.inflight() == 1

    time.sleep(0.01)
    assert q.reclaim() == 1
    assert q.inflight() == 0
    assert q.size() == 1


def test_full_policy_drop():
    cfg = QueueConfig(backend="memory", max_size=1, full_policy="drop")
    q = MemoryQueue(cfg)
    assert q.submit(make_record("c1", "a")) is True
    assert q.submit(make_record("c2", "b")) is False


def test_task_roundtrip_serialization():
    task = QueueTask(record=make_record("c1", "内容含表情😀", scenic_name="天山天池"))
    restored = QueueTask.from_json(task.to_json())
    assert restored.msg_id == task.msg_id
    assert restored.record.content == task.record.content
    assert restored.record.scenic_name == "天山天池"


def test_record_requires_comment_id():
    from opinion_labeling_engine.domain.models import CommentRecord

    with pytest.raises(ValueError):
        CommentRecord.from_dict({"content": "没有主键"})
