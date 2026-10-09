"""内容过滤：搜到的作品留不留。

对应需求：
  关闭        —— 搜到什么存什么，评论也照采
  开启+关键字  —— 只留标题/描述/标签里含搜索关键字的（默认，等同改造前）
  开启+补充词  —— 再加一组自己写的词，命中任意一个就留
"""
from __future__ import annotations

import pytest

from app.core.content_filter import ContentFilter, split_keywords, normalize


# ---------------------------------------------------------------------------
# 词表解析
# ---------------------------------------------------------------------------

def test_splits_on_every_separator_people_actually_type():
    """中英文逗号、顿号、分号、换行都当分隔符。

    用户不会记得该用哪个，而"填了没生效"这种问题查起来最费劲。
    """
    words = split_keywords("西山八大处，灵光寺、八大处公园;香界寺\n证果寺")
    assert words == ["西山八大处", "灵光寺", "八大处公园", "香界寺", "证果寺"]


def test_drops_blanks_and_dedupes_case_insensitively():
    assert split_keywords(" 八大处 , ,八大处, BaDaChu, badachu ") == ["八大处", "BaDaChu"]


def test_accepts_a_list_too():
    """库里存的是字符串，但前端可能直接传数组。"""
    assert split_keywords(["西山", "灵光寺"]) == ["西山", "灵光寺"]


def test_ignores_absurdly_long_words():
    assert split_keywords("八大处," + "长" * 200) == ["八大处"]


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def test_disabled_keeps_everything():
    rules = ContentFilter(enabled=False)
    assert rules.matches("点斑祛斑什么时候比较好", "八大处") is True


def test_keyword_mode_drops_unrelated_content():
    """线上原样：搜「八大处」搜出「点斑祛斑」这种蹭热度的内容。"""
    rules = ContentFilter(enabled=True, mode="keyword")
    assert rules.matches("八大处公园的秋天", "八大处") is True
    assert rules.matches("点斑祛斑什么时候比较好", "八大处") is False


def test_extra_keywords_are_or_not_and():
    """补充词是"或"：命中任意一个就留。

    做成"且"的话，一条文案里要同时出现「八大处」和「灵光寺」，
    能留下的内容会少得离谱。
    """
    rules = ContentFilter(
        enabled=True, mode="keyword_plus",
        extra_keywords=["灵光寺", "西山"],
    )
    assert rules.matches("灵光寺的佛牙塔", "八大处") is True       # 只命中补充词
    assert rules.matches("八大处公园秋游", "八大处") is True        # 只命中搜索词
    assert rules.matches("北京西山红叶", "八大处") is True
    assert rules.matches("点斑祛斑什么时候比较好", "八大处") is False


def test_extra_keywords_only_apply_in_plus_mode():
    """选了「只按搜索关键字」，补充词就不该生效——
    否则用户切回去之后会发现过滤突然变松了，却找不到原因。"""
    rules = ContentFilter(enabled=True, mode="keyword", extra_keywords=["灵光寺"])
    assert rules.matches("灵光寺的佛牙塔", "八大处") is False


def test_matching_is_case_insensitive():
    rules = ContentFilter(enabled=True, mode="keyword_plus", extra_keywords=["BaDaChu"])
    assert rules.matches("Visit badachu park", "八大处") is True


def test_no_words_to_match_means_keep():
    """主页采集没有搜索关键字。这时候拦下来等于一条都采不到。"""
    rules = ContentFilter(enabled=True, mode="keyword")
    assert rules.matches("随便什么内容", "") is True


# ---------------------------------------------------------------------------
# 解析 / 存库
# ---------------------------------------------------------------------------

def test_default_is_the_old_behaviour():
    """老任务的 params 里没有这个键。

    默认必须是"开启 + 只按搜索关键字"——默认关掉的话，
    所有老任务会在某次升级之后突然开始存一堆广告，而用户毫不知情。
    """
    rules = ContentFilter.from_params({})
    assert rules.enabled is True
    assert rules.mode == "keyword"
    assert rules.matches("点斑祛斑", "八大处") is False


def test_unknown_mode_falls_back():
    rules = ContentFilter.from_params({"content_filter": {"mode": "乱写"}})
    assert rules.mode == "keyword"


def test_normalize_round_trips():
    stored = normalize({"enabled": False, "mode": "keyword_plus",
                        "extra_keywords": "灵光寺，西山"})
    assert stored == {"enabled": False, "mode": "keyword_plus",
                      "extra_keywords": "灵光寺,西山",
                      # 新增的第三类词。老配置里没有这个键，读回来是空串
                      "exclude_keywords": ""}
    back = ContentFilter.from_params({"content_filter": stored})
    assert back.enabled is False and back.extra_keywords == ["灵光寺", "西山"]


def test_describe_tells_the_user_what_is_in_effect():
    assert "关闭" in ContentFilter(enabled=False).describe()
    assert "搜索关键字" in ContentFilter().describe()
    plus = ContentFilter(enabled=True, mode="keyword_plus", extra_keywords=["灵光寺"])
    assert "附关键字" in plus.describe() and "灵光寺" in plus.describe()
    # 选了补充词却没填，要说清楚等同于没填
    empty = ContentFilter(enabled=True, mode="keyword_plus", extra_keywords=[])
    assert "空的" in empty.describe()


# ---------------------------------------------------------------------------
# 和 runner 的接线
# ---------------------------------------------------------------------------

def test_runner_uses_the_configured_rules():
    from app.collectors.base import WorkItem
    from app.scheduler.runner import _work_matches_keyword

    work = WorkItem(channel="douyin", work_id="W1", scenic_id="S1", scenic_name="八大处公园",
                    title="灵光寺的佛牙塔", description="", label="")

    # 默认规则：不含搜索关键字 → 丢弃
    assert _work_matches_keyword(work, "八大处") is False
    # 配了补充词 → 留下
    plus = ContentFilter(enabled=True, mode="keyword_plus", extra_keywords=["灵光寺"])
    assert _work_matches_keyword(work, "八大处", plus) is True
    # 关掉过滤 → 全留
    assert _work_matches_keyword(work, "八大处", ContentFilter(enabled=False)) is True


def test_label_and_description_count_too():
    from app.collectors.base import WorkItem
    from app.scheduler.runner import _work_matches_keyword

    work = WorkItem(channel="douyin", work_id="W1", scenic_id="S1", scenic_name="x",
                    title="秋游记录", description="", label="#八大处")
    assert _work_matches_keyword(work, "八大处") is True


# ---------------------------------------------------------------------------
# 内容过滤在真实关键字上的行为：为什么"采到了却几乎不入库"
# ---------------------------------------------------------------------------
def test_scenic_keyword_with_suffix_drops_most_real_titles():
    """搜「盘山风景区」，真实笔记标题里多半只写「盘山」——会被整片丢掉。

    这不是 bug，是默认规则（标题/正文/标签必须含**完整**搜索关键字）。
    但它的后果很反直觉：日志显示"采到 20 条"，实际入库只有个位数，
    而且被丢的那些连评论都不会采。标题取自 2026-09-03 的真实运行。
    """
    from app.core.content_filter import ContentFilter

    rules = ContentFilter()
    real_titles = [
        "本周线下活动，天津盘山，蓟县古镇",
        "“早知有盘山，何必下江南”盘山简直就是户外人的天堂",
        "京郊自驾｜蓟县两天一夜攻略",
        "天津盘山风景区–乾隆皇帝真的来了32次吗",
    ]
    kept = [t for t in real_titles if rules.matches(t, "盘山风景区")]
    assert kept == ["天津盘山风景区–乾隆皇帝真的来了32次吗"], (
        "默认规则的行为变了。这条测试钉的是「搜索词带后缀时会丢掉大部分内容」，"
        "它是用户配补充词的理由"
    )

    # 加一个补充词「盘山」就全留下了——这正是该告诉用户的做法
    loose = ContentFilter(mode="keyword_plus", extra_keywords=["盘山"])
    kept2 = [t for t in real_titles if loose.matches(t, "盘山风景区")]
    assert len(kept2) == 3, f"补充词没起作用：{kept2}"
    assert "京郊自驾｜蓟县两天一夜攻略" not in kept2
