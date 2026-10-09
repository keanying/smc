"""关键词聚合单测。

要解决的问题：keyword_tags 是模型从原文抽的表述，同一个意思写法无数种——
「人太多 / 人多 / 人山人海 / 人太多了 / 人挤人」在词云里是五个低频词，
一个真正的高频问题就这么被切碎、在榜上看不见了。

聚合用**确定性字典**而不是让模型给归类词：这件事的全部价值就在一致，
模型每次可能给「人多」「人流量大」「拥挤」，等于没归并。
"""

from __future__ import annotations

import json

import pytest

from opinion_labeling_engine.config import ConfigError, LabelingConfig, load_config
from opinion_labeling_engine.domain.aggregation import KeywordAggregator
from opinion_labeling_engine.domain.enums import Sentiment
from opinion_labeling_engine.labeling.postprocess import LabelPostProcessor
from opinion_labeling_engine.llm.parser import RawLabel

from conftest import make_record


@pytest.fixture(scope="module")
def agg() -> KeywordAggregator:
    return KeywordAggregator.load(load_config(require_db=False).aggregation_path)


# ===================================================== 用户举的那两组例子
@pytest.mark.parametrize("keyword", [
    "人太多", "人多", "人太多了", "人山人海", "人挤人", "人很多", "人超多",
    "拥挤", "很挤", "人满为患", "爆满", "人潮", "乌泱泱", "到处都是人",
])
def test_crowding_words_all_collapse_to_one(agg, keyword):
    """这一组就是用户查那四条 SQL 的原因——它们必须归成同一个词。"""
    assert "人多拥挤" in agg.match(keyword), f"{keyword} 没归到「人多拥挤」"


@pytest.mark.parametrize("keyword", [
    "玩的很开心", "玩得很开心", "很好玩", "好玩", "太好玩了", "有意思",
    "开心", "很开心", "愉快", "不虚此行",
])
def test_happy_words_all_collapse_to_one(agg, keyword):
    assert "开心" in agg.match(keyword), f"{keyword} 没归到「开心」"


def test_the_four_sql_queries_now_need_only_one(agg):
    """用户原本要写四条 LIKE 才能捞全的词，聚合后一条 GROUP BY 就够了。"""
    originals = ["人太多", "人多", "人太多了", "人山人海"]
    assert {tuple(agg.match(k)) for k in originals} == {("人多拥挤",)}


# ===================================================== 匹配规则
def test_longer_synonym_wins_so_granularity_is_not_lost(agg):
    """「人山人海」不能因为含「人」就被粗粒度的词抢走。"""
    assert agg.match("人山人海") == ["人多拥挤"]


def test_normalization_handles_spaces_punctuation_and_fullwidth(agg):
    for variant in ["人 太 多", "人太多！", "人太多。", "人太多～"]:
        assert "人多拥挤" in agg.match(variant), variant


def test_regex_patterns_catch_constructed_phrases(agg):
    """「排队了整整三小时」这种组合说法穷举不完，靠正则兜住。"""
    assert "排队久" in agg.match("排队了整整三小时")
    assert "排队久" in agg.match("排队排了一小时")


def test_one_keyword_can_hit_several_groups(agg):
    hits = agg.match("排队两小时人还特别多")
    assert "排队久" in hits and "人多拥挤" in hits


def test_unknown_keyword_returns_nothing(agg):
    """归类词是封闭集合，没收录的词绝不能凭空造一个出来。"""
    assert agg.match("薛定谔的猫") == []


def test_empty_input(agg):
    assert agg.match("") == []
    assert agg.match("   ") == []
    assert agg.aggregate([]) == []


# ===================================================== 聚合
def test_aggregate_dedups(agg):
    """四个同义词只产出一个归类词——这正是聚合的意义。"""
    assert agg.aggregate(["人太多", "人山人海", "人挤人", "拥挤"]) == ["人多拥挤"]


def test_aggregate_order_is_stable_regardless_of_input_order(agg):
    """顺序必须稳定，否则落库后没法做等值比较、没法算 diff。"""
    a = agg.aggregate(["开心", "人太多", "风景很美"])
    b = agg.aggregate(["风景很美", "人太多", "开心"])
    c = agg.aggregate(["人太多", "风景很美", "开心"])
    assert a == b == c


def test_unmatched_are_tracked_for_growing_the_dictionary(agg):
    agg.reset_unmatched()
    agg.aggregate(["人太多", "某个没收录的词", "某个没收录的词", "另一个"])
    top = dict(agg.top_unmatched())
    assert top["某个没收录的词"] == 2
    assert top["另一个"] == 1
    agg.reset_unmatched()


# ===================================================== 否定守卫
# 这一节是词典最重要的护栏。正负两组词天然互相包含——
# 「不好吃」含「好吃」、「不拥挤」含「拥挤」、「态度不好」含「态度好」——
# 没有守卫的话一条好评会同时进正面榜和负面榜，词典越大对撞越多。
@pytest.mark.parametrize("keyword,expected", [
    ("不好吃", "东西难吃"),
    ("难吃", "东西难吃"),
    ("不拥挤", "不用排队"),
    ("人不多", "不用排队"),
    ("没排队", "不用排队"),
    ("态度不好", "服务态度差"),
    ("不干净", "环境脏乱"),
    ("不好玩", "项目少无聊"),
    ("不推荐", "不推荐"),
    ("不值这个价", "价格贵"),
    ("不贵", "性价比高"),
    ("风景不美", "景色一般"),
    ("交通不方便", "交通不便"),
    ("设施不完善", "设施陈旧"),
    ("厕所不臭", "厕所干净"),
])
def test_negation_lands_on_exactly_one_group(agg, keyword, expected):
    assert agg.match(keyword) == [expected], f"「{keyword}」归类跑偏了"


@pytest.mark.parametrize("keyword,expected", [
    ("不虚此行", "开心"),          # 「不」是词本身的一部分，不是否定
    ("不容错过", "值得再来"),
    ("人还特别多", "人多拥挤"),     # 「特别」里的「别」不是否定词
    ("人非常多", "人多拥挤"),       # 「非常」里的「非」不是否定词
])
def test_false_negation_is_not_treated_as_negation(agg, keyword, expected):
    assert expected in agg.match(keyword), f"「{keyword}」被误判成否定了"


def test_no_synonym_lands_in_an_opposite_polarity_group(agg):
    """把词典里**每一个同义词**过一遍匹配：

        1. 必须能命中自己那一组（写进去却匹配不上 = 白写）
        2. 绝不能命中情感相反的组（好评进负面榜 = 指标反了）

    加词加正则的时候这条最容易被破坏，所以让它在 CI 里守着。
    """
    import yaml

    from opinion_labeling_engine.config import load_config

    raw = yaml.safe_load(load_config(require_db=False).aggregation_path.read_text("utf-8"))
    polarity = {g["canonical"]: g.get("polarity", "") for g in raw["groups"]}

    problems = []
    for group in raw["groups"]:
        canonical = group["canonical"]
        for word in list(group.get("synonyms") or []) + [canonical]:
            hits = agg.match(word)
            if canonical not in hits:
                problems.append(f"「{word}」匹配不到自己所属的「{canonical}」，实得 {hits}")
            opposite = [h for h in hits
                        if polarity.get(h) and polarity[h] != polarity[canonical]]
            if opposite:
                problems.append(f"「{word}」（{canonical}）串到了反向组 {opposite}")

    assert not problems, "词典自检不通过：\n" + "\n".join(problems)


def test_every_group_declares_polarity_and_most_declare_a_dimension(agg):
    """dim 让归类词能和 dimension_tags 对上，看板才能从词下钻到维度。"""
    described = agg.describe()
    assert all(polarity for _, polarity, _, _ in described), "有组没写 polarity"
    with_dim = [c for c, _, dim, _ in described if dim]
    # 整体倾向类（开心/失望/推荐度/适宜人群）不挂维度，其余都要挂
    assert len(with_dim) >= len(described) - 12


# ===================================================== 词典本身
def test_dictionary_has_no_duplicate_canonicals():
    with pytest.raises(ConfigError):
        KeywordAggregator.from_raw({"groups": [
            {"canonical": "人多拥挤", "synonyms": ["人多"]},
            {"canonical": "人多拥挤", "synonyms": ["拥挤"]},
        ]})


def test_bad_regex_is_rejected_at_load_time():
    """坏正则要在启动时炸，不能等跑批跑到一半才发现。"""
    with pytest.raises(ConfigError):
        KeywordAggregator.from_raw({"groups": [
            {"canonical": "测试", "patterns": ["[unclosed"]},
        ]})


def test_empty_dictionary_is_rejected():
    with pytest.raises(ConfigError):
        KeywordAggregator.from_raw({"groups": []})


def test_canonical_matches_itself():
    """归类词本身也要能被匹配到，否则重跑聚合会把结果洗没。"""
    a = KeywordAggregator.from_raw({"groups": [{"canonical": "人多拥挤", "synonyms": []}]})
    assert a.match("人多拥挤") == ["人多拥挤"]


def test_disabled_group_is_excluded():
    a = KeywordAggregator.from_raw({"groups": [
        {"canonical": "启用的", "synonyms": ["甲"]},
        {"canonical": "停用的", "synonyms": ["乙"], "enabled": False},
    ]})
    assert a.canonicals == ["启用的"]
    assert a.match("乙") == []


def test_shipped_dictionary_covers_the_examples_in_the_request(agg):
    """交付的词典必须真的覆盖用户点名的那几个词。"""
    for word in ["人多", "拥挤", "人山人海", "爆满", "排队", "人潮", "开心"]:
        assert agg.match(word), f"词典里没有覆盖「{word}」"


# ===================================================== 与标注流程的接线
def test_postprocess_fills_aggregation_tags(taxonomy, agg):
    post = LabelPostProcessor(taxonomy, LabelingConfig(), agg)
    record = make_record(content="人太多了，还排队排了两小时")
    raw = RawLabel(
        sentiment="负向",
        keywords=[{"word": "人太多", "polarity": "负向"},
                  {"word": "排队排了两小时", "polarity": "负向"}],
    )
    result = post.build(record, raw, source_text=record.content)

    assert result.keyword_tags == ["人太多", "排队排了两小时"]     # 原始词原样保留
    assert result.aggregation_keyword_tags == ["人多拥挤", "排队久"]
    assert json.loads(result.aggregation_json()) == ["人多拥挤", "排队久"]


def test_aggregation_is_written_to_the_row(taxonomy, agg):
    post = LabelPostProcessor(taxonomy, LabelingConfig(), agg)
    record = make_record(content="玩的很开心")
    raw = RawLabel(sentiment="正向",
                   keywords=[{"word": "玩的很开心", "polarity": "正向"}])
    row = post.build(record, raw, source_text=record.content).to_row()
    assert row["aggregation_keyword_tags"] == '["开心"]'


def test_irrelevant_clears_aggregation_too(taxonomy, agg):
    """与景区无关 → 关键词清空，归类词当然也要一起清。"""
    post = LabelPostProcessor(taxonomy, LabelingConfig(), agg)
    record = make_record(content="家人们这条裙子才89，人太多了快抢")
    raw = RawLabel(relevant=False, sentiment="正向",
                   keywords=[{"word": "人太多", "polarity": "正向"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == []
    assert result.aggregation_keyword_tags == []


def test_without_aggregator_everything_else_still_works(taxonomy):
    """没配词典时只是不产出归类词，标注本身不受影响。"""
    post = LabelPostProcessor(taxonomy, LabelingConfig(), None)
    record = make_record(content="人太多了")
    raw = RawLabel(sentiment="负向", keywords=[{"word": "人太多", "polarity": "负向"}])
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["人太多"]
    assert result.aggregation_keyword_tags == []


def test_aggregation_only_sees_cleaned_keywords(taxonomy, agg):
    """聚合发生在关键词过滤之后：被判为幻觉的词不该混进归类结果。"""
    post = LabelPostProcessor(taxonomy, LabelingConfig(), agg)
    record = make_record(content="人太多了")
    raw = RawLabel(
        sentiment="负向",
        keywords=[{"word": "人太多", "polarity": "负向"},
                  {"word": "厕所太脏", "polarity": "负向"}],   # 原文没提厕所 → 幻觉
    )
    result = post.build(record, raw, source_text=record.content)
    assert result.keyword_tags == ["人太多"]
    assert result.aggregation_keyword_tags == ["人多拥挤"]      # 没有「环境脏乱」


# ===================================================== 回填脚本
def test_backfill_parses_messy_keyword_tags():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from backfill_aggregation import parse_keywords

    assert parse_keywords('["人太多","排队"]') == ["人太多", "排队"]
    assert parse_keywords(["人太多"]) == ["人太多"]
    assert parse_keywords("[]") == []
    assert parse_keywords(None) == []
    assert parse_keywords("") == []
    assert parse_keywords("不是 JSON") == []          # 脏数据不能让回填中断
    assert parse_keywords('{"不是":"数组"}') == []
