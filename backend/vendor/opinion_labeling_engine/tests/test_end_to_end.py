"""端到端单测：提交 → 队列 → 清洗 → （假）模型 → 后处理 → 写回。

用假模型与假仓储，验证整条链路的接线正确，以及几条最关键的行为：
    - 无效内容不调模型，直接中性落库
    - 正常内容按契约标注并写回
    - 模型输出坏掉时走中性兜底，不会卡住队列
    - 写库失败时任务重投，不会丢
"""

from __future__ import annotations

import json

import pytest

from opinion_labeling_engine.config import load_config
from opinion_labeling_engine.domain.enums import LabelSource, Sentiment
from opinion_labeling_engine.labeling.service import LabelingService
from opinion_labeling_engine.queues.memory import MemoryQueue
from opinion_labeling_engine.worker.engine import LabelingEngine

from conftest import FakeArkClient, RecordingRepository
from opinion_labeling_engine.storage.failure_store import MemoryFailureStore

# 需求里给的那一行真实样例数据（去掉五个标注字段）
SAMPLE_ROW = {
    "scenic_id": "PFTSCA01009835",
    "scenic_name": "天山天池",
    "channel": "ctrip",
    "work_id": "76471",
    "comment_level": "level_1",
    "comment_parent_id": "",
    "comment_id": "805353431",
    "commenter_id": "27885902",
    "image_list": "",
    "video_list": "",
    "location": "新疆",
    "content": "完美的一天",
    "likes": 0,
    "extra_content": json.dumps({
        "score": 5.0, "landscape_score": 5.0, "fun_score": 5.0,
        "price_quality_score": 5.0, "tourist_type": "朋友出游",
        "useful_count": 0, "from_type": "来自订单", "user_member": "钻石贵宾",
    }, ensure_ascii=False),
    "publish_time": "2026-08-27 09:18:41",
    "crawl_time": "2026-08-27 09:27:44",
    "commenter_name": "YoYo_5F7D3I9N",
    "root_comment_id": "805353431",
    "sub_comment_count": 0,
    "task_id": "126d1e9bd8d34fc89c3f4e4ff62cf5db",
}

POSITIVE_OUT = json.dumps({
    "relevant": True, "sentiment": "正向", "confidence": 0.75,
    "dimensions": [], "entities": [{"type": "游客类型", "value": "朋友结伴"}],
    "keywords": [{"word": "完美的一天", "polarity": "正向"}],
    "reason": "笼统好评",
}, ensure_ascii=False)

MIXED_OUT = json.dumps({
    "relevant": True, "sentiment": "负向", "confidence": 0.88,
    "dimensions": [
        {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": "正向"},
        {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
    ],
    "entities": [{"type": "设施设备", "value": "基础设施"}],
    "keywords": [
        {"word": "厕所太差", "polarity": "负向"},
        {"word": "风景很美", "polarity": "正向"},
    ],
    "reason": "厕所问题主导",
}, ensure_ascii=False)

AD_OUT = json.dumps({
    "relevant": False, "sentiment": "中性", "confidence": 0.95,
    "dimensions": [], "entities": [], "keywords": [], "reason": "带货广告",
}, ensure_ascii=False)


def build_engine(rules, repo=None, default=None):
    cfg = load_config(require_db=False)
    client = (FakeArkClient(rules=rules, default=default) if default is not None
              else FakeArkClient(rules=rules))
    service = LabelingService(cfg, client=client)
    queue = MemoryQueue(cfg.queue)
    engine = LabelingEngine(
        cfg, queue=queue, service=service,
        repository=repo if repo is not None else RecordingRepository(),
        failure_store=MemoryFailureStore(cfg.failure_store),
    )
    return engine, client


# ---------------------------------------------------------------------------
def test_sample_row_end_to_end():
    repo = RecordingRepository()
    engine, client = build_engine([("完美的一天", POSITIVE_OUT)], repo)
    engine.start(check_schema=False)
    assert engine.submit(SAMPLE_ROW) is True
    assert engine.wait_idle(timeout=10) is True
    engine.stop()

    result = repo.by_id("805353431")
    assert result is not None
    assert result.sentiment_label == "正向"
    assert result.sentiment_score == 1
    assert result.keyword_tags == ["完美的一天"]
    assert json.loads(result.entity_json()) == [{"type": "游客类型", "value": "朋友结伴"}]
    assert json.loads(result.dimension_json()) == []


def test_extra_content_hint_reaches_prompt():
    engine, client = build_engine([("完美的一天", POSITIVE_OUT)])
    engine.label_sync(SAMPLE_ROW)
    user_msg = client.calls[-1][-1]["content"][0]["text"]
    assert "景区名称：天山天池" in user_msg
    assert "总评分 5.0" in user_msg
    assert "朋友出游" in user_msg


def test_emoji_only_skips_model():
    engine, client = build_engine([])
    row = dict(SAMPLE_ROW, comment_id="emoji-1", content="😀😀😀")
    result = engine.label_sync(row)
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.source is LabelSource.RULE_INVALID
    assert result.keyword_tags == []
    assert client.calls == []          # 一次模型都没调，token 全省下来了


def test_mixed_comment_keeps_dimension_level_sentiment():
    engine, _ = build_engine([("厕所", MIXED_OUT)])
    row = dict(SAMPLE_ROW, comment_id="mix-1", content="风景很美但厕所太差、太脏")
    result = engine.label_sync(row)
    assert result.sentiment_score == -1
    dims = {(d.dim1, d.sentiment) for d in result.dimension_tags}
    assert ("游玩体验", 1) in dims
    assert ("设施环境", -1) in dims
    # 整体负向 → 关键词只留负向的
    assert result.keyword_tags == ["厕所太差"]


def test_irrelevant_comment_is_neutral():
    engine, _ = build_engine([("裙子", AD_OUT)])
    row = dict(SAMPLE_ROW, comment_id="ad-1",
               content="家人们谁懂啊，这条裙子才89，链接放评论区了")
    result = engine.label_sync(row)
    assert result.sentiment_score == 0
    assert result.sentiment_label == "中性"
    assert result.source is LabelSource.RULE_IRRELEVANT


def test_broken_model_output_raises_instead_of_writing_fake_neutral():
    """模型连续吐垃圾：修复轮也失败 → 抛 LabelingFailed，**不产生可写库的结果**。

    这是刻意的行为变更：以前写中性兜底，等于用假数据把行占了，
    以后既认不出它是失败的，下游指标还会当成一条真实的中性评价。
    """
    from opinion_labeling_engine.labeling.service import LabelingFailed

    engine, client = build_engine([("测试内容", "我不想输出 JSON")],
                                  default="修复轮我也不给 JSON")
    row = dict(SAMPLE_ROW, comment_id="bad-1", content="测试内容还行吧")
    with pytest.raises(LabelingFailed) as exc:
        engine.label_sync(row)
    assert exc.value.comment_id == "bad-1"
    assert len(client.calls) == 2       # 首轮 + 修复轮


def test_write_on_model_failure_true_restores_old_behavior():
    """配置开关：需要"宁可有值也不要 NULL"时可以退回兜底写库。"""
    from dataclasses import replace

    from opinion_labeling_engine.labeling.service import LabelingService

    cfg = load_config(require_db=False)
    cfg = replace(cfg, labeling=replace(cfg.labeling, write_on_model_failure=True))
    service = LabelingService(cfg, client=FakeArkClient(
        rules=[("测试内容", "我不想输出 JSON")], default="修复轮我也不给 JSON"))
    engine = LabelingEngine(cfg, queue=MemoryQueue(cfg.queue), service=service,
                            repository=RecordingRepository(),
                            failure_store=MemoryFailureStore(cfg.failure_store))
    result = engine.label_sync(dict(SAMPLE_ROW, comment_id="bad-2", content="测试内容还行吧"))
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.source is LabelSource.FALLBACK


def test_repair_round_recovers():
    """首轮格式坏、修复轮正常 → 应拿到正常结果。"""
    cfg = load_config(require_db=False)

    class FlakyClient(FakeArkClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        def complete(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                return super().complete(messages, **kwargs).__class__(
                    text="抱歉，我先解释一下……", model="fake", latency_ms=1)
            from opinion_labeling_engine.llm.client import LLMResponse
            return LLMResponse(text=f"```json\n{POSITIVE_OUT}\n```", model="fake", latency_ms=1)

    service = LabelingService(cfg, client=FlakyClient())
    engine = LabelingEngine(cfg, queue=MemoryQueue(cfg.queue), service=service,
                            repository=RecordingRepository(),
                            failure_store=MemoryFailureStore(cfg.failure_store))
    result = engine.label_sync(dict(SAMPLE_ROW, comment_id="rep-1", content="完美的一天"))
    assert result.sentiment is Sentiment.POSITIVE
    assert engine.service.stats.snapshot()["parse_repaired"] == 1


def test_write_failure_requeues_then_succeeds():
    repo = RecordingRepository()
    repo.fail_times = 1                 # 第一批写库失败，任务应被重投
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)], repo)
    engine.start(check_schema=False)
    engine.submit(SAMPLE_ROW)
    assert engine.wait_idle(timeout=15) is True
    engine.stop()
    assert repo.by_id("805353431") is not None


def test_submit_many_skips_invalid_rows():
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    rows = [SAMPLE_ROW, {"content": "没有 comment_id"},
            dict(SAMPLE_ROW, comment_id="x2")]
    assert engine.submit_many(rows) == 2
    assert engine.dropped == 1


def test_stats_shape():
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    engine.label_sync(SAMPLE_ROW)
    stats = engine.stats()
    for key in ("submitted", "processed", "pending", "inflight", "dead",
                "total", "labeled", "skipped_invalid"):
        assert key in stats
