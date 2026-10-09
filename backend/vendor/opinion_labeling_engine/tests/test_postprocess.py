"""后处理单测：需求里的四条硬约束，每条都必须有测试守着。"""

from __future__ import annotations

import pytest

from opinion_labeling_engine.config import LabelingConfig
from opinion_labeling_engine.domain.enums import LabelSource, Sentiment
from opinion_labeling_engine.labeling.postprocess import LabelPostProcessor
from opinion_labeling_engine.llm.parser import RawLabel

from conftest import make_record


@pytest.fixture()
def post(taxonomy) -> LabelPostProcessor:
    return LabelPostProcessor(taxonomy, LabelingConfig())


# ===================================================== 要求 1：关键词方向一致
def test_positive_label_keeps_only_positive_keywords(post):
    record = make_record(content="风景很美，但是排队太久了，总体还是值得")
    raw = RawLabel(
        sentiment="正向",
        confidence=0.8,
        keywords=[
            {"word": "风景很美", "polarity": "正向"},
            {"word": "排队太久", "polarity": "负向"},
            {"word": "还是值得", "polarity": "正向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment is Sentiment.POSITIVE
    assert result.keyword_tags == ["风景很美", "还是值得"]
    assert "排队太久(负向)" in result.dropped["keyword_polarity"]


def test_negative_label_keeps_only_negative_keywords(post):
    record = make_record(content="景色还行，但厕所太脏，工作人员态度差")
    raw = RawLabel(
        sentiment="负向",
        keywords=[
            {"word": "景色还行", "polarity": "正向"},
            {"word": "厕所太脏", "polarity": "负向"},
            {"word": "态度差", "polarity": "负向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["厕所太脏", "态度差"]


def test_neutral_label_keeps_only_neutral_keywords(post):
    record = make_record(content="周末去的，人比较多，坐了观光车")
    raw = RawLabel(
        sentiment="中性",
        keywords=[
            {"word": "人比较多", "polarity": "中性"},
            {"word": "非常好玩", "polarity": "正向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["人比较多"]


# ===================================================== 要求 2：维度必须合法
def test_illegal_dimension_is_dropped(post):
    record = make_record(content="停车场太小了")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[
            {"dim1": "交通接驳", "dim2": "外部交通", "dim3": "停车场", "sentiment": "负向"},
            {"dim1": "玄学体验", "dim2": "风水", "dim3": "气场", "sentiment": "负向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert len(result.dimension_tags) == 1
    assert result.dimension_tags[0].to_dict() == {
        "dim1": "交通接驳", "dim2": "外部交通", "dim3": "停车场", "sentiment": -1,
    }
    assert "玄学体验/风水/气场" in result.dropped["dimension"]


def test_missing_dim3_is_completed_when_unique(post):
    """「排队时长」下只有一条无三级路径，模型漏写 dim3 也应能对齐。"""
    record = make_record(content="排队排了两小时")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[{"dim1": "游玩体验", "dim2": "排队时长", "sentiment": "负向"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.dimension_tags[0].dim3 == ""


def test_dimension_sentiment_is_independent_of_overall(post):
    """「风景很美但厕所太差」：整体负向，但景色维度必须是正向。"""
    record = make_record(content="风景很美但厕所太差、太脏")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[
            {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": "正向"},
            {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
        ],
        keywords=[{"word": "厕所太差", "polarity": "负向"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment is Sentiment.NEGATIVE
    scores = {(d.dim2, d.sentiment) for d in result.dimension_tags}
    assert ("景色观赏", 1) in scores
    assert ("基础设施", -1) in scores


def test_duplicate_dimensions_are_deduped(post):
    record = make_record(content="厕所很脏，厕所真的脏")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[
            {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
            {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert len(result.dimension_tags) == 1


def test_dimension_count_is_capped(taxonomy):
    cfg = LabelingConfig(dimension_max_count=2)
    post = LabelPostProcessor(taxonomy, cfg)
    record = make_record(content="景色好，厕所脏，停车难，讲解棒")
    raw = RawLabel(
        sentiment="中性",
        dimensions=[
            {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": "正向"},
            {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
            {"dim1": "交通接驳", "dim2": "外部交通", "dim3": "停车场", "sentiment": "负向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert len(result.dimension_tags) == 2


# ===================================================== 要求 3：无关内容中性
def test_irrelevant_is_forced_neutral_and_cleared(post):
    record = make_record(content="家人们谁懂啊，这条裙子才89，链接放评论区")
    raw = RawLabel(
        relevant=False,
        sentiment="正向",
        confidence=0.9,
        dimensions=[{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                     "sentiment": "正向"}],
        keywords=[{"word": "才89", "polarity": "正向"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.sentiment_score == 0
    assert result.dimension_tags == []
    assert result.keyword_tags == []
    assert result.source is LabelSource.RULE_IRRELEVANT


# ===================================================== 要求 4：关键词无幻觉
def test_single_char_keyword_is_dropped(post):
    record = make_record(content="好")
    raw = RawLabel(sentiment="正向", keywords=[{"word": "好", "polarity": "正向"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == []
    assert "好" in result.dropped["keyword_too_short"]


def test_hallucinated_keyword_is_dropped(post):
    record = make_record(content="风景不错")
    raw = RawLabel(
        sentiment="正向",
        keywords=[
            {"word": "风景不错", "polarity": "正向"},
            {"word": "缆车平稳", "polarity": "正向"},     # 原文里根本没提
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["风景不错"]
    assert "缆车平稳" in result.dropped["keyword_hallucination"]


def test_blacklist_keyword_is_dropped(post):
    record = make_record(content="这个景区感觉还可以")
    raw = RawLabel(
        sentiment="中性",
        keywords=[{"word": "景区", "polarity": "中性"}, {"word": "感觉", "polarity": "中性"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == []


def test_sentence_like_keyword_is_dropped(post):
    record = make_record(content="排队太久了，服务态度也差，不推荐")
    raw = RawLabel(
        sentiment="负向",
        keywords=[
            {"word": "排队太久了，服务态度也差", "polarity": "负向"},
            {"word": "不推荐", "polarity": "负向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["不推荐"]


def test_paraphrased_keyword_is_kept(post):
    """「排了很久的队」→「排队久」：字符都在原文里，属于合理压缩，应保留。"""
    record = make_record(content="排了很久的队，累死了")
    raw = RawLabel(sentiment="负向", keywords=[{"word": "排队久", "polarity": "负向"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["排队久"]


def test_contained_keyword_is_dropped(post):
    record = make_record(content="排队久，排队久到怀疑人生")
    raw = RawLabel(
        sentiment="负向",
        keywords=[{"word": "排队", "polarity": "负向"}, {"word": "排队久", "polarity": "负向"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["排队久"]


def test_keyword_count_is_capped(taxonomy):
    cfg = LabelingConfig(keyword_max_count=2)
    post = LabelPostProcessor(taxonomy, cfg)
    record = make_record(content="风景美 服务好 交通便利 价格实惠")
    raw = RawLabel(
        sentiment="正向",
        keywords=[{"word": w, "polarity": "正向"}
                  for w in ("风景美", "服务好", "交通便利", "价格实惠")],
    )
    result = post.build(record, raw, source_text=record.content)
    assert len(result.keyword_tags) == 2


# ===================================================== 其它
def test_unrecognizable_sentiment_falls_back_to_neutral(post):
    record = make_record(content="还行吧")
    raw = RawLabel(sentiment="超级无敌", confidence=0.9)
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.confidence == 0.0


def test_illegal_entity_is_dropped_but_open_type_is_kept(post):
    record = make_record(content="瑶琳仙境值得一去")
    raw = RawLabel(
        sentiment="正向",
        entities=[
            {"type": "景区地名", "value": "瑶琳仙境"},
            {"type": "外星生物", "value": "章鱼哥"},
            {"type": "票务类型", "value": "月票"},        # 票务类型下没有「月票」
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert [e.to_dict() for e in result.entity_tags] == [
        {"type": "景区地名", "value": "瑶琳仙境"},
    ]


def test_entity_slash_variant_is_normalized(post):
    record = make_record(content="和老公一起来的")
    raw = RawLabel(sentiment="正向",
                   entities=[{"type": "游客类型", "value": "情侣/夫妻"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.entity_tags[0].value == "情侣夫妻"


def test_majority_arbiter(taxonomy):
    cfg = LabelingConfig(overall_sentiment_arbiter="majority")
    post = LabelPostProcessor(taxonomy, cfg)
    record = make_record(content="厕所脏，停车难，景色一般般还行")
    raw = RawLabel(
        sentiment="正向",
        dimensions=[
            {"dim1": "设施环境", "dim2": "基础设施", "dim3": "厕所", "sentiment": "负向"},
            {"dim1": "交通接驳", "dim2": "外部交通", "dim3": "停车场", "sentiment": "负向"},
            {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": "正向"},
        ],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment is Sentiment.NEGATIVE


def test_output_json_matches_sample_format(post):
    """落库 JSON 必须与需求给的样例数据结构完全一致。"""
    record = make_record(content="瑶琳仙境的风景太差了")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                     "sentiment": "负向"}],
        entities=[{"type": "景区地名", "value": "瑶琳仙境"}],
        keywords=[{"word": "风景太差", "polarity": "负向"}],
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.sentiment_label == "负向"
    assert result.sentiment_score == -1
    assert result.dimension_json() == (
        '[{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光", "sentiment": -1}]'
    )
    assert result.entity_json() == '[{"type": "景区地名", "value": "瑶琳仙境"}]'
    assert result.keyword_json() == '["风景太差"]'
