"""三类关键字：主搜索 / 附留存 / 过滤丢弃。

判定顺序是固定的，而且**过滤关键字必须在留存判定之后**：
一条内容哪怕完美命中附关键字，只要出现过滤词照样丢。
顺序反了的话语义变成"先排除再看相关性"，结果不一样。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.content_filter import (            # noqa: E402
    MAX_AUX_KEYWORDS, MAX_EXCLUDE_KEYWORDS, MODE_KEYWORD, MODE_KEYWORD_PLUS,
    ContentFilter, split_keywords,
)


def _rules(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("mode", MODE_KEYWORD_PLUS)
    return ContentFilter(**kw)


# ---------------------------------------------------------------- 过滤关键字
def test_exclude_hits_are_dropped():
    r = _rules(extra_keywords=["盘山"], exclude_keywords=["代运营"])
    assert r.matches("盘山民宿代运营", "盘山风景区") is True      # 留存这一层是过的
    assert r.blocked_by("盘山民宿代运营") == "代运营"             # 但被过滤词一票否决


def test_exclude_returns_which_word_hit():
    """返回**命中的那个词**，不是 True/False。

    用户可能配了 200 个过滤词，日志只说"被过滤词丢弃"的话，
    误伤了根本不知道该删哪个，只能一个个删着试。
    """
    r = _rules(exclude_keywords=["代运营", "加微信", "涨粉"])
    assert r.blocked_by("私信我加微信") == "加微信"
    assert r.blocked_by("正常的游记") == ""


def test_empty_exclude_list_blocks_nothing():
    """⚠️ 不配就完全不起作用——不能因为列表是空的就把什么都拦下来。"""
    r = _rules(extra_keywords=["盘山"], exclude_keywords=[])
    assert r.blocked_by("盘山代运营加微信涨粉") == ""


def test_exclude_ignores_enabled_flag():
    """内容过滤关掉 ≠ 想把明确排除掉的广告也存进来。

    enabled 管的是"要不要按相关性筛"，过滤词是"这些东西我一概不要"，两件事。
    """
    r = ContentFilter(enabled=False, exclude_keywords=["代运营"])
    assert r.matches("任何内容", "任何词") is True     # 相关性这层放行
    assert r.blocked_by("盘山代运营") == "代运营"       # 过滤词照样拦


def test_exclude_is_case_insensitive():
    r = _rules(exclude_keywords=["VLOG"])
    assert r.blocked_by("我的vlog日记") == "VLOG"


# ---------------------------------------------------------------- 顺序
@pytest.mark.parametrize("text,keep,blocked", [
    ("盘山红叶真美", True, ""),              # 附关键字命中、无过滤词 → 存
    ("八大处的红叶", False, ""),             # 附关键字没命中 → 丢（不相关）
    ("盘山民宿代运营", True, "代运营"),       # 相关但命中过滤词 → 丢
    ("八大处代运营", False, "代运营"),        # 两层都不过
])
def test_three_stages(text, keep, blocked):
    r = _rules(extra_keywords=["盘山"], exclude_keywords=["代运营"])
    assert r.matches(text, "盘山风景区") is keep
    assert r.blocked_by(text) == blocked


# ---------------------------------------------------------------- 序列化
def test_exclude_survives_round_trip():
    r = _rules(extra_keywords=["盘山"], exclude_keywords=["代运营", "加微信"])
    back = ContentFilter.from_params({"content_filter": r.to_params()})
    assert back.exclude_keywords == ["代运营", "加微信"]
    assert back.extra_keywords == ["盘山"]


def test_old_params_without_exclude_still_load():
    """老任务的 params 里没有 exclude_keywords，不能因此炸掉。"""
    back = ContentFilter.from_params({"content_filter": {
        "enabled": True, "mode": "keyword_plus", "extra_keywords": "盘山"}})
    assert back.exclude_keywords == []
    assert back.extra_keywords == ["盘山"]


def test_separators_are_all_accepted():
    """中英文逗号、顿号、分号、换行都当分隔符——用户不会记得该用哪个。"""
    assert split_keywords("代运营，加微信、涨粉;引流\n刷单") == [
        "代运营", "加微信", "涨粉", "引流", "刷单"]


def test_caps_differ_per_kind():
    """两类的上限**不一样**：附关键字 700，过滤关键字 200。

    ⚠️ 合成一个常量是错的：拿 200 去截附关键字，第 201 个之后的景区别名
    会被安静丢掉，现象是"我明明配了这个别名，怎么还是没采到"。
    """
    assert MAX_AUX_KEYWORDS == 700
    assert MAX_EXCLUDE_KEYWORDS == 200

    many = ",".join(f"w{i}" for i in range(1000))
    assert len(split_keywords(many, MAX_AUX_KEYWORDS)) == 700
    assert len(split_keywords(many, MAX_EXCLUDE_KEYWORDS)) == 200
    # 不传 limit 时按大的那份来，别把附关键字截到 200
    assert len(split_keywords(many)) == 700


def test_from_params_applies_the_right_cap_to_each_list():
    """解析任务配置时，两个列表各按各的上限截断。"""
    many = ",".join(f"w{i}" for i in range(1000))
    r = ContentFilter.from_params({"content_filter": {
        "enabled": True, "mode": MODE_KEYWORD_PLUS,
        "extra_keywords": many, "exclude_keywords": many}})
    assert len(r.extra_keywords) == 700
    assert len(r.exclude_keywords) == 200


def test_describe_mentions_exclude():
    """配置说明里要能看出过滤词生效了——用户改完最想确认的就是这个。"""
    text = _rules(extra_keywords=["盘山"], exclude_keywords=["代运营"]).describe()
    assert "过滤关键字" in text and "代运营" in text


def test_describe_when_filter_off_still_mentions_exclude():
    text = ContentFilter(enabled=False, exclude_keywords=["代运营"]).describe()
    assert "已关闭" in text and "过滤关键字" in text
