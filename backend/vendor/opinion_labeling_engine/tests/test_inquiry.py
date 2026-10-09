"""咨询提问类评论的标注（要求 5）。

起因是你从库里捞出来的两条真实误标：

    最近人多吗              →  标出了 ["人多"]
    人多吗？我们4岁多可去么   →  标出了 ["人多", "4岁多"]

这两条都不是评价，是出行前的打听。但「人多」经聚合会变成「人多拥挤」，
直接进拥挤度统计——问的人还没去过，却给景区记了一笔"嫌人多"。
问「人多吗」的人远比抱怨人多的人多，所以这类噪声会系统性地
把热门景区的拥挤度指标顶高。

修的方式是两层：提示词里加 R6 降低发生率，后处理按规则兜底保证下限。
下面的用例盯的是后处理这一层——它不依赖模型听不听话。
"""

from __future__ import annotations

import pytest

from opinion_labeling_engine.config import LabelingConfig
from opinion_labeling_engine.domain.enums import LabelSource, Sentiment
from opinion_labeling_engine.domain.inquiry import (
    declarative_text,
    interrogative_spans,
    is_interrogative,
    is_pure_inquiry,
    is_quantity_only,
    split_clauses,
)
from opinion_labeling_engine.labeling.postprocess import LabelPostProcessor
from opinion_labeling_engine.llm.parser import RawLabel

from conftest import make_record


def _post(taxonomy, agg=None, **overrides):
    return LabelPostProcessor(taxonomy, LabelingConfig(**overrides), agg)


def _build(post, text, sentiment="负向", words=()):
    record = make_record(content=text)
    raw = RawLabel(
        sentiment=sentiment,
        keywords=[{"word": w, "polarity": sentiment} for w in words],
    )
    return post.build(record, raw, source_text=text)


# ===================================================== 你给的两个真实案例
def test_the_exact_case_1(taxonomy, aggregator):
    """最近人多吗 → 不该抽出任何关键词，情感中性。"""
    result = _build(_post(taxonomy, aggregator), "最近人多吗", words=["人多"])
    assert result.keyword_tags == []
    assert result.aggregation_keyword_tags == []          # 不会污染「人多拥挤」
    assert result.sentiment is Sentiment.NEUTRAL
    assert result.source is LabelSource.RULE_INQUIRY


def test_the_exact_case_2(taxonomy, aggregator):
    """人多吗？我们4岁多可去么 → 两个词都要拦掉，原因还不一样。"""
    result = _build(_post(taxonomy, aggregator),
                    "人多吗？我们4岁多可去么", words=["人多", "4岁多"])
    assert result.keyword_tags == []
    assert result.sentiment is Sentiment.NEUTRAL
    assert "人多" in result.dropped.get("keyword_inquiry", [])        # 疑问对象
    assert "4岁多" in result.dropped.get("keyword_quantity_only", [])  # 年龄不是评价


# ===================================================== 不能误伤的：真评价
@pytest.mark.parametrize("text,words,keep", [
    ("人太多了，排队排了两小时", ["人太多", "排队排了两小时"], ["人太多", "排队排了两小时"]),
    ("风景很美，请问几点关门？", ["风景很美"], ["风景很美"]),           # 半问半评
    ("这里人真多，建议早点来", ["人真多"], ["人真多"]),
    ("人多吗？反正我去的时候人是真的多", ["人多"], ["人多"]),            # 陈述部分说了同一件事
])
def test_real_complaints_survive(taxonomy, aggregator, text, words, keep):
    """拦疑问句不能顺手把真抱怨一起拦了，否则等于把负面舆情捂掉。"""
    result = _build(_post(taxonomy, aggregator), text, words=words)
    assert result.keyword_tags == keep


def test_half_question_half_review_keeps_its_sentiment(taxonomy, aggregator):
    """只要有一句是陈述评价，整条就不是纯咨询，情感必须留住。"""
    result = _build(_post(taxonomy, aggregator), "风景很美，请问几点关门？",
                    sentiment="正向", words=["风景很美"])
    assert result.sentiment is Sentiment.POSITIVE
    assert result.source is LabelSource.LLM
    assert result.aggregation_keyword_tags == ["风景优美"]


# ===================================================== 疑问句识别本身
@pytest.mark.parametrize("text", [
    "最近人多吗", "要预约吗", "几点开门", "怎么去", "门票多少钱",
    "有没有优惠", "可以带宠物吗", "请问停车方便吗", "4岁小孩能玩么",
    "值不值得去", "现在去合适吗？",
])
def test_recognized_as_question(text):
    assert is_interrogative(text)


@pytest.mark.parametrize("text", [
    "太美了吧", "真好看呢", "人是真的多", "值得一去", "排队排了两小时",
    "这个地方我去过很多次了",
])
def test_not_mistaken_for_question(text):
    """『吧』『呢』是感叹语气，不配问号就不算提问——不然好评会被当咨询清空。"""
    assert not is_interrogative(text)


def test_clause_split_keeps_offsets():
    clauses = split_clauses("风景很美，请问几点关门？")
    assert [c.text for c in clauses] == ["风景很美，", "请问几点关门？"]
    assert clauses[0].start == 0 and clauses[1].end == len("风景很美，请问几点关门？")
    assert not clauses[0].question and clauses[1].question


def test_pure_inquiry_only_when_every_clause_is_a_question():
    assert is_pure_inquiry("人多吗？我们4岁多可去么")
    assert not is_pure_inquiry("风景很美，请问几点关门？")
    assert not is_pure_inquiry("")


def test_spans_and_declarative_are_complements():
    text = "人多吗？我去的时候人是真的多"
    assert interrogative_spans(text) == [(0, 4)]
    assert declarative_text(text) == "我去的时候人是真的多"


# ===================================================== 数量词
@pytest.mark.parametrize("word", [
    "4岁多", "两小时", "50块", "3个人", "2026", "五点半", "2个小时",
    "三小时以上", "10分钟", "1米2",
])
def test_quantity_words_are_rejected(word):
    assert is_quantity_only(word)


@pytest.mark.parametrize("word", [
    "排队两小时", "五星好评", "一般般", "十分推荐", "一日游", "人多",
    "等了两小时", "门票100贵",
])
def test_evaluative_words_with_numbers_survive(word):
    """「排队两小时」词头是评价对象，不能因为带数字就被当成纯数量。"""
    assert not is_quantity_only(word)


def test_quantity_rule_can_be_turned_off(taxonomy):
    post = _post(taxonomy, None, drop_quantity_only_keywords=False)
    result = _build(post, "我们4岁多", words=["4岁多"])
    assert result.keyword_tags == ["4岁多"]


# ===================================================== 开关
def test_inquiry_handling_can_be_turned_off(taxonomy, aggregator):
    """留了开关：万一下游就是想统计"有多少人在问人多不多"，可以关掉。"""
    post = _post(taxonomy, aggregator,
                 drop_inquiry_keywords=False, neutralize_pure_inquiry=False)
    result = _build(post, "最近人多吗", words=["人多"])
    assert result.keyword_tags == ["人多"]
    assert result.sentiment is Sentiment.NEGATIVE


def test_pure_inquiry_clears_dimensions_but_keeps_entities(taxonomy, aggregator):
    """维度带情感，必须清；实体是无褒贬的事实抽取，留着有用——
    "大家都在问什么"本身就是有价值的运营信息。"""
    record = make_record(content="要预约吗？门票多少钱")
    raw = RawLabel(
        sentiment="负向",
        dimensions=[{"dim1": "游玩体验", "dim2": "性价比",
                     "dim3": "门票价格", "sentiment": "负向"}],
        entities=[{"type": "票务类型", "value": "门票"}],
        keywords=[{"word": "门票", "polarity": "负向"}],
    )
    result = _post(taxonomy, aggregator).build(record, raw, source_text=record.content)
    assert result.dimension_tags == []
    assert [e.value for e in result.entity_tags] == ["门票"]
    assert result.sentiment is Sentiment.NEUTRAL
