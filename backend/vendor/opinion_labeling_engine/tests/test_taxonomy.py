"""标签体系单测：白名单、情感归一、提示词素材。"""

from __future__ import annotations

from opinion_labeling_engine.domain.enums import Sentiment


def test_six_top_level_dimensions(taxonomy):
    dim1s = {node.dim1 for node in taxonomy.nodes}
    assert dim1s == {"游玩体验", "服务质量", "设施环境", "餐饮购物", "安全秩序", "交通接驳"}


def test_dimension_paths_are_unique(taxonomy):
    paths = [node.path for node in taxonomy.nodes]
    assert len(paths) == len(set(paths))


def test_whitelist(taxonomy):
    assert taxonomy.is_valid_dimension("游玩体验", "景色观赏", "自然风光")
    assert taxonomy.is_valid_dimension("游玩体验", "排队时长", "")
    assert not taxonomy.is_valid_dimension("游玩体验", "景色观赏", "外星风光")
    assert not taxonomy.is_valid_dimension("玄学体验", "风水", "")


def test_resolve_dimension_completes_unique_child(taxonomy):
    assert taxonomy.resolve_dimension("游玩体验", "排队时长", "等待时间") == (
        "游玩体验", "排队时长", "")


def test_resolve_dimension_fuzzy_third_level(taxonomy):
    assert taxonomy.resolve_dimension("设施环境", "智慧设施", "wifi信号") == (
        "设施环境", "智慧设施", "WiFi")


def test_resolve_dimension_returns_none_for_garbage(taxonomy):
    assert taxonomy.resolve_dimension("不存在", "也不存在", "") is None


def test_disabled_entities_are_excluded(taxonomy):
    # 「具体人员」「餐厅名称」在需求里标注为本期暂不涉及
    assert "具体人员" not in taxonomy.entity_types["工作人员"]
    assert "餐厅名称" not in taxonomy.entity_types["餐饮商品"]
    assert "岗位" in taxonomy.entity_types["工作人员"]


def test_open_entity_type(taxonomy):
    assert "景区地名" in taxonomy.open_entity_types
    assert taxonomy.is_valid_entity("景区地名", "任意一个没见过的景点名")
    assert not taxonomy.is_valid_entity("票务类型", "任意一个没见过的票种")


def test_sentiment_normalization(taxonomy):
    assert taxonomy.normalize_sentiment("正向") is Sentiment.POSITIVE
    assert taxonomy.normalize_sentiment("正面") is Sentiment.POSITIVE
    assert taxonomy.normalize_sentiment("positive") is Sentiment.POSITIVE
    assert taxonomy.normalize_sentiment(1) is Sentiment.POSITIVE
    assert taxonomy.normalize_sentiment("负面") is Sentiment.NEGATIVE
    assert taxonomy.normalize_sentiment(-1) is Sentiment.NEGATIVE
    assert taxonomy.normalize_sentiment("中立") is Sentiment.NEUTRAL
    assert taxonomy.normalize_sentiment(0) is Sentiment.NEUTRAL
    assert taxonomy.normalize_sentiment("不知道") is None
    assert taxonomy.normalize_sentiment(None) is None
    assert taxonomy.normalize_sentiment(7) is None


def test_sentiment_scores():
    assert Sentiment.POSITIVE.score == 1
    assert Sentiment.NEUTRAL.score == 0
    assert Sentiment.NEGATIVE.score == -1


def test_prompt_blocks_contain_every_path(taxonomy):
    block = taxonomy.prompt_dimension_block()
    for node in taxonomy.nodes:
        assert node.dim2 in block
        if node.dim3:
            assert node.dim3 in block
    entity_block = taxonomy.prompt_entity_block()
    for etype in taxonomy.entity_types:
        assert etype in entity_block


def test_blacklist_loaded(taxonomy):
    assert "景区" in taxonomy.keyword_blacklist
    assert "感觉" in taxonomy.keyword_blacklist
