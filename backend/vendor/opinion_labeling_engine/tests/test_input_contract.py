"""入参契约单测：六字段入参 + 写回主键校验。

调用方传这六个字段：
    channel / work_id / scenic_id / scenic_name / comment_id / content
其中 channel + scenic_id + comment_id 是写回主键，缺任一必须在入队时就被拒绝；
work_id / scenic_name / content 缺失只告警不拦截。
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

# —— 调用方实际会传的入参 ——
MIN_ROW = {
    "channel": "ctrip",
    "work_id": "76471",
    "scenic_id": "PFTSCA01009835",
    "scenic_name": "天山天池",
    "comment_id": "805353431",
    "content": "完美的一天",
}

POSITIVE_OUT = json.dumps({
    "relevant": True, "sentiment": "正向", "confidence": 0.75,
    "dimensions": [], "entities": [],
    "keywords": [{"word": "完美的一天", "polarity": "正向"}],
    "reason": "笼统好评",
}, ensure_ascii=False)


def build_engine(rules, repo=None):
    cfg = load_config(require_db=False)
    client = FakeArkClient(rules=rules)
    service = LabelingService(cfg, client=client)
    engine = LabelingEngine(
        cfg, queue=MemoryQueue(cfg.queue), service=service,
        repository=repo if repo is not None else RecordingRepository(),
        failure_store=MemoryFailureStore(cfg.failure_store),
    )
    return engine, client


# =============================================================== 入参可用
def test_six_fields_work_end_to_end():
    repo = RecordingRepository()
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)], repo)
    engine.start(check_schema=False)
    assert engine.submit(MIN_ROW) is True
    assert engine.wait_idle(timeout=10) is True
    engine.stop()

    result = repo.by_id("805353431")
    assert result is not None
    assert result.sentiment_label == "正向"
    assert result.sentiment_score == 1
    assert result.keyword_tags == ["完美的一天"]


def test_scenic_name_reaches_prompt():
    """景区名必须进提示词——它是"是否与景区相关"判定的依据。"""
    engine, client = build_engine([("完美的一天", POSITIVE_OUT)])
    engine.label_sync(MIN_ROW)
    user_msg = client.calls[-1][-1]["content"][0]["text"]
    assert "景区名称：天山天池" in user_msg
    assert "来源渠道：ctrip" in user_msg
    # 没给 extra_content 时不应该凭空出现"补充信息"段
    assert "补充信息" not in user_msg


def test_work_id_is_carried_but_not_sent_to_model():
    """work_id 要跟着记录走（便于下钻回溯），但没必要塞进提示词浪费 token。"""
    from opinion_labeling_engine.domain.models import CommentRecord

    record = CommentRecord.from_dict(MIN_ROW)
    assert record.work_id == "76471"

    engine, client = build_engine([("完美的一天", POSITIVE_OUT)])
    engine.label_sync(MIN_ROW)
    assert "76471" not in client.calls[-1][-1]["content"][0]["text"]


def test_optional_fields_are_truly_optional():
    """likes / publish_time / extra_content 全不给，也要能跑通。"""
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    result = engine.label_sync(MIN_ROW)
    assert result.sentiment is Sentiment.POSITIVE


def test_unknown_extra_fields_are_ignored():
    """调用方多传字段不应该报错。"""
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    row = dict(MIN_ROW, 某个我们没定义的字段="whatever", another=123)
    assert engine.label_sync(row).sentiment is Sentiment.POSITIVE


# =============================================================== 主键校验
@pytest.mark.parametrize("missing", ["channel", "scenic_id", "comment_id"])
def test_missing_key_column_is_rejected_at_submit(missing):
    engine, _ = build_engine([])
    row = dict(MIN_ROW)
    row[missing] = ""
    with pytest.raises(ValueError) as exc:
        engine.submit(row)
    assert missing in str(exc.value)


@pytest.mark.parametrize("missing", ["channel", "scenic_id"])
def test_whitespace_only_key_is_rejected(missing):
    engine, _ = build_engine([])
    row = dict(MIN_ROW)
    row[missing] = "   "
    with pytest.raises(ValueError):
        engine.submit(row)


def test_submit_many_skips_bad_rows_and_keeps_going():
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    rows = [
        MIN_ROW,
        dict(MIN_ROW, comment_id="c2", channel=""),      # 缺渠道
        dict(MIN_ROW, comment_id="c3"),
        {"content": "连 comment_id 都没有"},
    ]
    assert engine.submit_many(rows) == 2
    assert engine.dropped == 2


@pytest.mark.parametrize("field", ["scenic_name", "work_id"])
def test_missing_non_key_field_warns_but_passes(caplog, field):
    """非主键字段缺了只告警不拦截。"""
    engine, _ = build_engine([])
    row = dict(MIN_ROW)
    row[field] = ""
    with caplog.at_level("WARNING"):
        assert engine.submit(row) is True
    assert field in caplog.text


def test_key_columns_must_be_declared_in_input_contract():
    """配置自检：写回主键必须是调用方会传的字段，否则永远匹配不到行。"""
    from opinion_labeling_engine.config import ConfigError, StorageConfig

    bad = StorageConfig(
        key_columns=["channel", "scenic_id", "comment_id", "commenter_id"],
        required_input_fields=["channel", "scenic_id", "comment_id"],
        columns={k: k for k in ("sentiment_label", "sentiment_score", "dimension_tags",
                                "entity_tags", "keyword_tags")},
    )
    with pytest.raises(ConfigError) as exc:
        bad.validate()
    assert "commenter_id" in str(exc.value)


def test_label_sync_without_write_does_not_require_keys():
    """只看效果不写库时，允许只给 content 试标。"""
    engine, _ = build_engine([("测试", POSITIVE_OUT)])
    result = engine.label_sync({"comment_id": "tmp", "content": "测试一下风景"})
    assert result is not None


# =============================================================== 要求 1：过滤
@pytest.mark.parametrize("content", [
    "😀😀😀",
    "[比心][比心][doge]",
    "。。。。。。",
    "！！！???",
    "666666",
    "哈哈哈哈哈哈",
    "   ",
])
def test_emoji_and_symbol_only_default_to_neutral_without_calling_model(content):
    """全表情 / 特殊字符 → 直接中性，一次模型都不调。"""
    engine, client = build_engine([])
    result = engine.label_sync(dict(MIN_ROW, comment_id=f"x-{len(content)}", content=content))
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.sentiment_score == 0
    assert result.dimension_tags == []
    assert result.keyword_tags == []
    assert result.source is LabelSource.RULE_INVALID
    assert client.calls == []


def test_irrelevant_content_defaults_to_neutral():
    """与景区无关（广告 / 闲聊）→ 模型判 relevant=false → 强制中性。"""
    ad_out = json.dumps({
        "relevant": False, "sentiment": "正向", "confidence": 0.95,
        "dimensions": [{"dim1": "游玩体验", "dim2": "景色观赏",
                        "dim3": "自然风光", "sentiment": "正向"}],
        "entities": [], "keywords": [{"word": "速抢", "polarity": "正向"}],
        "reason": "带货广告",
    }, ensure_ascii=False)
    engine, _ = build_engine([("裙子", ad_out)])
    result = engine.label_sync(dict(MIN_ROW, comment_id="ad-1",
                                    content="家人们谁懂啊，这条裙子才89，速抢"))
    assert result.sentiment_label == "中性"
    assert result.sentiment_score == 0
    assert result.dimension_tags == []
    assert result.keyword_tags == []
    assert result.source is LabelSource.RULE_IRRELEVANT


# =============================================================== 写回按三主键
def test_update_sql_uses_channel_scenic_id_comment_id():
    from opinion_labeling_engine.storage.repository import LabelRepository

    cfg = load_config(require_db=False)
    sql, order = LabelRepository._build_update_sql(cfg.storage)

    assert sql.startswith("UPDATE `src_opinion_social_work_comment_di` SET ")
    for col in ("sentiment_label", "sentiment_score", "dimension_tags",
                "entity_tags", "keyword_tags"):
        assert f"`{col}` = %s" in sql
    assert "WHERE `channel` = %s AND `scenic_id` = %s AND `comment_id` = %s" in sql
    assert order[-3:] == ["key:channel", "key:scenic_id", "key:comment_id"]


def test_update_params_bind_in_order():
    from opinion_labeling_engine.domain.models import CommentRecord
    from opinion_labeling_engine.storage.repository import LabelRepository

    cfg = load_config(require_db=False)
    repo = LabelRepository.__new__(LabelRepository)
    repo._cfg = cfg.storage
    repo._sql, repo._value_order = LabelRepository._build_update_sql(cfg.storage)

    record = CommentRecord.from_dict(MIN_ROW)
    engine, _ = build_engine([("完美的一天", POSITIVE_OUT)])
    result = engine.label_sync(MIN_ROW)

    params = repo._params(record, result)
    assert params[-3:] == ("ctrip", "PFTSCA01009835", "805353431")
    assert params[0] == "正向"
    assert params[1] == 1
