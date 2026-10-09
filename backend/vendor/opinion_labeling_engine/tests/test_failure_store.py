"""失败池单测：标注失败不写库、进 Redis 等重标。

核心约定（用户要求）：
    - 模型标注失败 → **不写标注字段**，转入失败池
    - 只有 AI 真标出来的才写库
    - 规则命中的中性（纯表情 / 景区无关）不是失败，照常写库
"""

from __future__ import annotations

import json
import time

import pytest

from opinion_labeling_engine.config import ConfigError, load_config
from opinion_labeling_engine.domain.enums import LabelSource, Sentiment
from opinion_labeling_engine.domain.models import CommentRecord
from opinion_labeling_engine.labeling.service import LabelingFailed, LabelingService
from opinion_labeling_engine.queues.memory import MemoryQueue
from opinion_labeling_engine.storage.failure_store import (FailureRecord,
                                                           MemoryFailureStore,
                                                           NullFailureStore,
                                                           create_failure_store)
from opinion_labeling_engine.worker.engine import LabelingEngine

from conftest import FakeArkClient, RecordingRepository

ROW = {
    "channel": "ctrip", "work_id": "76471", "scenic_id": "PFTSCA01009835",
    "scenic_name": "天山天池", "comment_id": "805353431", "content": "风景真的不错，值得一去",
}
GOOD_OUT = json.dumps({
    "relevant": True, "sentiment": "正向", "confidence": 0.9,
    "dimensions": [], "entities": [],
    "keywords": [{"word": "风景真的不错", "polarity": "正向"}], "reason": "好评",
}, ensure_ascii=False)
GARBAGE = "我就是不给你 JSON"


def build(rules, *, default=None, repo=None, store=None, fail_store_cfg=None):
    cfg = load_config(require_db=False)
    client = (FakeArkClient(rules=rules, default=default) if default is not None
              else FakeArkClient(rules=rules))
    service = LabelingService(cfg, client=client)
    engine = LabelingEngine(
        cfg,
        queue=MemoryQueue(cfg.queue),
        service=service,
        repository=repo if repo is not None else RecordingRepository(),
        failure_store=store or MemoryFailureStore(fail_store_cfg or cfg.failure_store),
    )
    return engine, client


# =================================================== 失败不写库
def test_model_failure_never_reaches_the_database():
    repo = RecordingRepository()
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([("风景", GARBAGE)], default=GARBAGE, repo=repo, store=store)

    engine.start(check_schema=False)
    engine.submit(ROW)
    engine.wait_idle(timeout=20)
    engine.stop()

    assert repo.rows == [], "标注失败的评论绝不能写进标注字段"
    assert store.count() == 1


def test_failure_record_keeps_the_whole_comment_for_retry():
    """失败池要存整条评论，重标时不用回 MySQL 再查一遍。"""
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([("风景", GARBAGE)], default=GARBAGE, store=store)
    engine.start(check_schema=False)
    engine.submit(ROW)
    engine.wait_idle(timeout=20)
    engine.stop()

    item = store.peek()[0]
    assert item.record["comment_id"] == "805353431"
    assert item.record["content"] == ROW["content"]
    assert item.record["scenic_name"] == "天山天池"
    assert "ParseError" in item.error or "LLMError" in item.error


def test_successful_label_still_writes():
    repo = RecordingRepository()
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([("风景", GOOD_OUT)], repo=repo, store=store)
    engine.start(check_schema=False)
    engine.submit(ROW)
    engine.wait_idle(timeout=20)
    engine.stop()

    assert repo.by_id("805353431").sentiment is Sentiment.POSITIVE
    assert store.count() == 0


@pytest.mark.parametrize("content,expected_source", [
    ("😀😀😀", LabelSource.RULE_INVALID),
    ("。。。。。", LabelSource.RULE_INVALID),
])
def test_rule_neutral_is_a_result_not_a_failure(content, expected_source):
    """纯表情判中性是**确定的结论**，照常写库，不该进失败池。"""
    repo = RecordingRepository()
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([], repo=repo, store=store)
    engine.start(check_schema=False)
    engine.submit(dict(ROW, comment_id="e1", content=content))
    engine.wait_idle(timeout=20)
    engine.stop()

    assert repo.by_id("e1").source is expected_source
    assert store.count() == 0


# =================================================== 重试后才入池
def test_queue_retries_before_giving_up():
    """失败先走队列重试，重试用尽才进失败池——瞬时抖动不该立刻记一笔。"""
    cfg = load_config(require_db=False)
    store = MemoryFailureStore(cfg.failure_store)
    engine, client = build([("风景", GARBAGE)], default=GARBAGE, store=store)
    engine.start(check_schema=False)
    engine.submit(ROW)
    engine.wait_idle(timeout=30)
    engine.stop()

    # queue.max_attempts 次标注尝试，最后只落一条失败记录
    assert store.count() == 1
    assert store.peek()[0].attempts == 1


def test_repeated_failures_accumulate_on_one_record():
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    record = CommentRecord.from_dict(ROW)
    store.record(record, "第一次失败")
    store.record(record, "第二次失败")
    store.record(record, "第三次失败")

    assert store.count() == 1                 # 同一条评论不产生三条记录
    item = store.peek()[0]
    assert item.attempts == 3
    assert item.error == "第三次失败"


# =================================================== take / 重标
def test_take_removes_items_so_two_runners_do_not_collide():
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    for i in range(5):
        store.record(CommentRecord.from_dict(dict(ROW, comment_id=f"c{i}")), "boom")

    first = store.take(3, min_age_seconds=0)
    second = store.take(3, min_age_seconds=0)
    assert len(first) == 3
    assert len(second) == 2
    assert store.count() == 0
    assert not ({i.comment_id for i in first} & {i.comment_id for i in second})


def test_take_respects_min_age():
    """刚失败的先晾一会儿——模型正在抽风时立刻重试只是白烧 token。"""
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    store.record(CommentRecord.from_dict(ROW), "刚刚失败的")
    assert store.take(10, min_age_seconds=3600) == []
    assert store.count() == 1
    assert len(store.take(10, min_age_seconds=0)) == 1


def test_retry_failed_relabels_and_writes():
    """重标：失败池里的评论重新入队，这次模型正常了就该写库。"""
    repo = RecordingRepository()
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([("风景", GOOD_OUT)], repo=repo, store=store)

    store.record(CommentRecord.from_dict(ROW), "上次失败了")
    engine.start(check_schema=False)
    report = engine.retry_failed(min_age_seconds=0)
    engine.stop()

    assert report["taken"] == 1
    assert report["submitted"] == 1
    assert report["remaining"] == 0
    assert repo.by_id("805353431").sentiment is Sentiment.POSITIVE


def test_retry_failed_puts_it_back_when_it_fails_again():
    """重标又失败 → 回到池子里，attempts 继续累加，不会丢。"""
    store = MemoryFailureStore(load_config(require_db=False).failure_store)
    engine, _ = build([("风景", GARBAGE)], default=GARBAGE, store=store)

    store.record(CommentRecord.from_dict(ROW), "第一次失败")
    engine.start(check_schema=False)
    report = engine.retry_failed(min_age_seconds=0)
    engine.stop()

    assert report["taken"] == 1
    assert store.count() == 1                 # 又回来了
    assert store.peek()[0].attempts == 2      # 次数累加


def test_retry_failed_requires_a_started_engine():
    engine, _ = build([])
    with pytest.raises(RuntimeError):
        engine.retry_failed()


# =================================================== 容量与过期
def test_capacity_drops_the_oldest():
    from dataclasses import replace

    cfg = replace(load_config(require_db=False).failure_store, max_records=3)
    store = MemoryFailureStore(cfg)
    for i in range(5):
        store.record(CommentRecord.from_dict(dict(ROW, comment_id=f"c{i}")), "boom")
        time.sleep(0.001)
    assert store.count() == 3
    assert "c0" not in {i.comment_id for i in store.peek(10)}


def test_prune_clears_expired():
    from dataclasses import replace

    cfg = replace(load_config(require_db=False).failure_store, ttl_days=1)
    store = MemoryFailureStore(cfg)
    store.record(CommentRecord.from_dict(ROW), "boom")
    store.peek()[0].last_failed_at = time.time() - 2 * 86400
    assert store.prune() == 1
    assert store.count() == 0


def test_ttl_zero_keeps_forever():
    from dataclasses import replace

    cfg = replace(load_config(require_db=False).failure_store, ttl_days=0)
    store = MemoryFailureStore(cfg)
    store.record(CommentRecord.from_dict(ROW), "boom")
    store.peek()[0].last_failed_at = 0
    assert store.prune() == 0
    assert store.count() == 1


# =================================================== 序列化 / 工厂 / 配置
def test_failure_record_roundtrip():
    item = FailureRecord(record=CommentRecord.from_dict(ROW).to_dict(),
                         error="出错了", attempts=2)
    restored = FailureRecord.from_json(item.to_json())
    assert restored.key() == item.key()
    assert restored.attempts == 2
    assert restored.to_comment().content == ROW["content"]


def test_key_includes_channel_and_scenic_id():
    """主键要和写回的 key_columns 一致，避免跨渠道撞号。"""
    a = FailureRecord(record=dict(ROW, channel="ctrip"))
    b = FailureRecord(record=dict(ROW, channel="xhs"))
    assert a.key() != b.key()


def test_null_store_warns_because_failures_would_vanish(caplog):
    store = NullFailureStore()
    with caplog.at_level("WARNING"):
        store.record(CommentRecord.from_dict(ROW), "boom")
    assert "失败池已关闭" in caplog.text
    assert store.count() == 0


def test_factory_honours_backend():
    from dataclasses import replace

    cfg = load_config(require_db=False).failure_store
    assert isinstance(create_failure_store(replace(cfg, backend="memory")),
                      MemoryFailureStore)
    assert isinstance(create_failure_store(replace(cfg, backend="none")),
                      NullFailureStore)
    assert isinstance(create_failure_store(replace(cfg, enabled=False)),
                      NullFailureStore)
    with pytest.raises(ValueError):
        create_failure_store(replace(cfg, backend="mongodb"))


def test_config_defaults_to_redis():
    """用户明确要求用 Redis 记失败——默认就该是 redis，而不是会丢的 memory。"""
    cfg = load_config(require_db=False)
    assert cfg.failure_store.enabled is True
    assert cfg.failure_store.backend == "redis"
    assert cfg.labeling.write_on_model_failure is False


def test_config_rejects_bad_backend():
    from dataclasses import replace

    cfg = load_config(require_db=False).failure_store
    with pytest.raises(ConfigError):
        replace(cfg, backend="mongodb").validate()
