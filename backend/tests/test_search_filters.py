"""搜索的排序与时间范围筛选。

重点验证两件事：
  1. 各平台支持什么、不支持什么，如实反映（快手接口真的没有这两个参数）
  2. 不管服务端支不支持，**客户端时间窗兜底**都能保证落库的数据在范围内
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core import search_filters as sf

NOW = datetime(2026, 8, 27, 12, 0, 0)


# ---------------------------------------------------------------------------
# 各平台的能力
# ---------------------------------------------------------------------------

def test_kuaishou_has_no_server_side_sort():
    """快手的搜索接口没有排序参数——不是没做，是接口就没有。

    MediaCrawler 的 visionSearchPhoto GraphQL 只收
    keyword / pcursor / searchSessionId / page / webPageArea。
    """
    assert sf.SORT_OPTIONS["kuaishou"] == []
    assert sf.options_for("kuaishou")["sort_supported"] is False
    # 但时间范围仍然可以设，靠本地过滤
    assert sf.options_for("kuaishou")["time_supported"] is True
    assert "本地过滤" in sf.options_for("kuaishou")["note"]


@pytest.mark.parametrize("channel,expected", [
    ("douyin", {"general", "latest", "most_like"}),
    ("xiaohongshu", {"general", "latest", "most_like"}),
    ("weibo", {"general", "latest", "most_like"}),
])
def test_sort_options_per_channel(channel, expected):
    assert {o["value"] for o in sf.SORT_OPTIONS[channel]} == expected


def test_no_platform_filters_arbitrary_date_range_natively():
    """任意日期区间**没有一个平台的接口原生支持**，全靠客户端时间窗兜底。

    ⚠️ 微博曾经被当成支持的，理由是"移动端 containerid 就是把桌面版
    s.weibo.com 的查询串搬过来的"——那是推断，不是核过的事实。
    timescope 是桌面站的参数，m.weibo.cn 的容器接口不认；带上它，
    「宝珠洞索道」这类小众词会退化成一张 card_type=4 的空提示卡，
    一条都搜不到，而不带就能搜到。
    """
    custom = {"publish_within": "custom", "start_date": "2026-08-01", "end_date": "2026-08-20"}
    for channel in ("weibo", "douyin", "xiaohongshu", "kuaishou"):
        assert sf.resolve(channel, custom, now=NOW).time_supported_natively is False, channel


def test_weibo_custom_range_is_reported_as_local_filtering():
    """界面和日志都得说清楚是本地过滤，别让用户以为接口筛过了。"""
    filters = sf.resolve(
        "weibo",
        {"publish_within": "custom", "start_date": "2026-08-01", "end_date": "2026-08-20"},
        now=NOW,
    )
    assert "本地过滤" in filters.describe()
    assert "timescope" in sf.options_for("weibo")["note"]


def test_preset_window_is_native_for_douyin_and_xhs():
    preset = {"publish_within": "week"}
    assert sf.resolve("douyin", preset, now=NOW).time_supported_natively is True
    assert sf.resolve("xiaohongshu", preset, now=NOW).time_supported_natively is True
    assert sf.resolve("kuaishou", preset, now=NOW).time_supported_natively is False


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def test_defaults_to_unlimited():
    filters = sf.resolve("douyin", {}, now=NOW)
    assert filters.sort == "general"
    assert filters.publish_within == "unlimited"
    assert filters.window is None


def test_unknown_sort_falls_back_to_general():
    """快手没有排序选项，误传了也不能让它进到请求里。"""
    assert sf.resolve("kuaishou", {"sort": "latest"}, now=NOW).sort == "general"
    assert sf.resolve("douyin", {"sort": "most_collect"}, now=NOW).sort == "general"


@pytest.mark.parametrize("preset,days", [("day", 1), ("week", 7), ("half_year", 180)])
def test_preset_windows(preset, days):
    filters = sf.resolve("douyin", {"publish_within": preset}, now=NOW)
    start, end = filters.window
    assert end == NOW
    assert start == NOW - timedelta(days=days)


def test_custom_date_range():
    filters = sf.resolve("weibo", {
        "publish_within": "custom",
        "start_date": "2026-08-01", "end_date": "2026-08-27",
    }, now=NOW)
    start, end = filters.window
    assert start == datetime(2026, 8, 1, 0, 0, 0)
    # 结束日期要覆盖到当天最后一秒，否则当天的内容会被漏掉
    assert end == datetime(2026, 8, 27, 23, 59, 59)


def test_dates_win_over_preset():
    """填了日期就按日期来——用户明确给了区间，那就是他的意图。"""
    filters = sf.resolve("weibo", {
        "publish_within": "week", "start_date": "2026-01-01", "end_date": "2026-01-31",
    }, now=NOW)
    assert filters.publish_within == "custom"
    assert filters.window[0] == datetime(2026, 1, 1)


def test_reversed_dates_are_swapped():
    filters = sf.resolve("weibo", {
        "start_date": "2026-08-27", "end_date": "2026-08-01",
    }, now=NOW)
    assert filters.window[0] < filters.window[1]


def test_open_ended_ranges():
    only_start = sf.resolve("weibo", {"start_date": "2026-08-01"}, now=NOW)
    assert only_start.window[0] == datetime(2026, 8, 1)
    assert only_start.window[1] == NOW

    only_end = sf.resolve("weibo", {"end_date": "2026-08-10"}, now=NOW)
    assert only_end.window[1] == datetime(2026, 8, 10, 23, 59, 59)


def test_garbage_dates_degrade_to_unlimited():
    filters = sf.resolve("weibo", {"start_date": "不是日期"}, now=NOW)
    assert filters.window is None
    assert filters.publish_within == "unlimited"


def test_channel_params_override_top_level():
    params = {
        "sort": "general", "publish_within": "unlimited",
        "channel_params": {"douyin": {"sort": "latest", "publish_within": "day"}},
    }
    douyin = sf.resolve("douyin", params, now=NOW)
    weibo = sf.resolve("weibo", params, now=NOW)
    assert douyin.sort == "latest" and douyin.publish_within == "day"
    assert weibo.sort == "general" and weibo.window is None


# ---------------------------------------------------------------------------
# 客户端时间窗过滤：不管平台支不支持，落库的都得在范围内
# ---------------------------------------------------------------------------

def test_in_window():
    filters = sf.resolve("kuaishou", {
        "start_date": "2026-08-01", "end_date": "2026-08-27",
    }, now=NOW)
    assert filters.in_window(datetime(2026, 8, 15)) is True
    assert filters.in_window(datetime(2026, 8, 1, 0, 0, 0)) is True     # 含左端
    assert filters.in_window(datetime(2026, 8, 27, 23, 59, 59)) is True  # 含右端
    assert filters.in_window(datetime(2026, 7, 31, 23, 59)) is False
    assert filters.in_window(datetime(2026, 8, 28)) is False


def test_unparseable_publish_time_is_kept():
    """发布时间解析不出来的一律放行。

    宁可多收一条，也不要因为某个平台的时间格式没认出来，
    就把整批数据默默丢掉——那种丢法用户根本发现不了。
    """
    filters = sf.resolve("kuaishou", {"publish_within": "day"}, now=NOW)
    assert filters.in_window(None) is True


def test_no_window_keeps_everything():
    filters = sf.resolve("douyin", {}, now=NOW)
    assert filters.in_window(datetime(2000, 1, 1)) is True
    assert filters.in_window(None) is True


def test_older_than_window_only_when_really_older():
    filters = sf.resolve("kuaishou", {
        "start_date": "2026-08-01", "end_date": "2026-08-27",
    }, now=NOW)
    assert filters.older_than_window(datetime(2026, 7, 1)) is True
    assert filters.older_than_window(datetime(2026, 8, 15)) is False
    # 比窗口还**新**的不算"更早"，不能拿它触发提前停
    assert filters.older_than_window(datetime(2026, 9, 1)) is False
    assert filters.older_than_window(None) is False


def test_early_stop_only_when_sorted_by_time():
    """只有按"最新发布"排的时候，越过窗口下界才意味着后面都更早。

    综合排序下顺序是乱的，看到一条老内容不代表后面没有新的。
    """
    by_time = sf.resolve("douyin", {"sort": "latest", "publish_within": "week"}, now=NOW)
    by_general = sf.resolve("douyin", {"sort": "general", "publish_within": "week"}, now=NOW)
    assert by_time.sorted_by_time is True
    assert by_general.sorted_by_time is False


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def test_describe_says_where_the_filtering_happens():
    native = sf.resolve("douyin", {"sort": "latest", "publish_within": "week"}, now=NOW)
    assert "最新发布" in native.describe()
    assert "接口侧筛选" in native.describe()

    local = sf.resolve("kuaishou", {"publish_within": "week"}, now=NOW)
    assert "本地过滤" in local.describe(), "快手是本地过滤，别让用户以为接口筛过了"


def test_describe_without_window():
    assert "时间不限" in sf.resolve("douyin", {}, now=NOW).describe()


def test_all_options_covers_four_channels():
    channels = {o["channel"] for o in sf.all_options()}
    assert channels == {"douyin", "kuaishou", "xiaohongshu", "weibo"}


def test_to_dict_is_json_safe():
    import json

    payload = sf.resolve("douyin", {"publish_within": "week"}, now=NOW).to_dict()
    json.dumps(payload)   # 不该抛
    assert payload["native_time_filter"] is True    # 抖音的预设档是接口侧筛的

    weibo = sf.resolve("weibo", {"start_date": "2026-08-01"}, now=NOW).to_dict()
    json.dumps(weibo)
    assert weibo["native_time_filter"] is False     # 微博只能本地过滤
