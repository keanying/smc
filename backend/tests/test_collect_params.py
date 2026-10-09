"""按平台的采集数量解析。

覆盖需求 4：作品型平台（抖音/快手/小红书/微博）设「作品数 + 每作品评论数」，
点评型平台（携程/同程）只设「点评总数」。
"""
from __future__ import annotations

import pytest

from app.core import collect_params as cp
from app.core.config import load_config


@pytest.fixture()
def cfg():
    return load_config(use_cache=False)


def test_falls_back_to_config_defaults(cfg):
    limits = cp.resolve(cfg, {}, "douyin")
    assert limits["max_works"] == cfg.get("crawl.default_max_works")
    assert limits["max_comments_per_work"] == cfg.get("crawl.default_max_comments_per_work")
    assert limits["max_comment_level"] == cfg.get("crawl.max_comment_level")


def test_top_level_params_override_config(cfg):
    limits = cp.resolve(cfg, {"max_works": 7, "max_comments_per_work": 9}, "douyin")
    assert limits["max_works"] == 7
    assert limits["max_comments_per_work"] == 9


def test_channel_params_beat_top_level(cfg):
    params = {
        "max_works": 100,
        "max_comments_per_work": 500,
        "channel_params": {"douyin": {"max_works": 20, "max_comments_per_work": 30}},
    }
    douyin = cp.resolve(cfg, params, "douyin")
    weibo = cp.resolve(cfg, params, "weibo")

    assert (douyin["max_works"], douyin["max_comments_per_work"]) == (20, 30)
    # 没被覆盖的平台仍然用顶层值
    assert (weibo["max_works"], weibo["max_comments_per_work"]) == (100, 500)


def test_partial_channel_override_keeps_the_rest(cfg):
    params = {
        "max_works": 100, "max_comments_per_work": 500,
        "channel_params": {"kuaishou": {"max_works": 5}},
    }
    limits = cp.resolve(cfg, params, "kuaishou")
    assert limits["max_works"] == 5
    assert limits["max_comments_per_work"] == 500


def test_zero_means_unlimited_not_missing(cfg):
    """0 是「不限」，不能被当成「没填」而回退到默认值。"""
    limits = cp.resolve(cfg, {"channel_params": {"weibo": {"max_works": 0}}}, "weibo")
    assert limits["max_works"] == 0


@pytest.mark.parametrize("channel", ["ctrip", "tongcheng"])
def test_poi_channels_use_max_comments(cfg, channel):
    """点评型平台只有一层：max_comments 落到每条合成作品的评论上限上。"""
    params = {"channel_params": {channel: {"max_comments": 1234}}}
    limits = cp.resolve(cfg, params, channel)

    assert limits["max_comments_per_work"] == 1234
    # 一个 POI 目标就是一条合成作品，作品数不该再限制
    assert limits["max_works"] == 0
    # 点评就是全部内容，不允许被「不采评论」关掉
    assert limits["collect_comments"] is True


def test_poi_channel_default_comes_from_its_own_config_key(cfg):
    limits = cp.resolve(cfg, {}, "ctrip")
    assert limits["max_comments_per_work"] == cfg.get("crawl.default_max_comments_per_poi")


def test_poi_channel_ignores_max_works_from_top_level(cfg):
    """顶层给作品型平台设的 max_works 不该泄漏到点评型平台。"""
    limits = cp.resolve(cfg, {"max_works": 3}, "ctrip")
    assert limits["max_works"] == 0


def test_collect_comments_can_be_turned_off_per_channel(cfg):
    params = {"channel_params": {"weibo": {"collect_comments": False}}}
    assert cp.resolve(cfg, params, "weibo")["collect_comments"] is False
    assert cp.resolve(cfg, params, "douyin")["collect_comments"] is True


# ---------------------------------------------------------------------------
# normalize：存库前的清洗
# ---------------------------------------------------------------------------

def test_normalize_drops_unselected_channels():
    params = {"channel_params": {"douyin": {"max_works": 5}, "weibo": {"max_works": 8}}}
    cleaned = cp.normalize(params, ["douyin"])
    assert set(cleaned["channel_params"]) == {"douyin"}


def test_normalize_drops_unknown_channels():
    params = {"channel_params": {"douyin": {"max_works": 5}, "tiktok": {"max_works": 8}}}
    cleaned = cp.normalize(params, ["douyin", "tiktok"])
    assert set(cleaned["channel_params"]) == {"douyin"}


def test_normalize_strips_keys_the_channel_cannot_use():
    """给携程配 max_works 没有意义，别存进去污染编辑页。"""
    params = {"channel_params": {"ctrip": {"max_works": 50, "max_comments": 100}}}
    cleaned = cp.normalize(params, ["ctrip"])
    assert cleaned["channel_params"]["ctrip"] == {"max_comments": 100}


def test_normalize_coerces_strings_from_the_form():
    params = {
        "max_works": "30",
        "channel_params": {"douyin": {"max_comments_per_work": "40", "collect_comments": "false"}},
    }
    cleaned = cp.normalize(params, ["douyin"])
    assert cleaned["max_works"] == 30
    assert cleaned["channel_params"]["douyin"]["max_comments_per_work"] == 40
    assert cleaned["channel_params"]["douyin"]["collect_comments"] is False


# ---------------------------------------------------------------------------
# collect_engine：建任务时选「自动 / 仅接口 / 仅浏览器」
# ---------------------------------------------------------------------------

def test_engine_defaults_to_hybrid(cfg):
    """老任务的 params 里没有这个键，必须按「混合」走，不能报错也不能变成仅接口。"""
    assert cp.resolve(cfg, {}, "douyin")["collect_engine"] == "hybrid"


def test_engine_can_be_set_per_channel(cfg):
    params = {
        "collect_engine": "api",
        "channel_params": {"douyin": {"collect_engine": "human"}},
    }
    assert cp.resolve(cfg, params, "douyin")["collect_engine"] == "human"
    # 没覆盖的平台仍然用顶层值
    assert cp.resolve(cfg, params, "kuaishou")["collect_engine"] == "api"


def test_engine_falls_back_on_garbage(cfg):
    """前端传了不认识的值，回落到默认，而不是原样带进采集器。"""
    assert cp.resolve(cfg, {"collect_engine": "turbo"}, "douyin")["collect_engine"] == "hybrid"


@pytest.mark.parametrize("legacy,expect", [("auto", "hybrid"), ("browser", "human"), ("api", "api")])
def test_legacy_engine_values_still_work(cfg, legacy, expect):
    """改名前存进库的 auto/browser 要映射过来。

    不映射的话它们会被当成非法值回落到默认，
    用户明明选过"仅浏览器"却被悄悄改成混合——而且没有任何提示。
    """
    assert cp.resolve(cfg, {"collect_engine": legacy}, "douyin")["collect_engine"] == expect


def test_poi_channels_always_api(cfg):
    """携程/同程没有拟人采集器，顶层设成 human 也不该传下去。"""
    assert cp.resolve(cfg, {"collect_engine": "human"}, "ctrip")["collect_engine"] == "api"


def test_normalize_lowercases_and_validates_engine():
    params = {
        "collect_engine": "HUMAN",
        "channel_params": {"douyin": {"collect_engine": "api"},
                           "kuaishou": {"collect_engine": "nope"},
                           "xiaohongshu": {"collect_engine": "browser"}},
    }
    cleaned = cp.normalize(params, ["douyin", "kuaishou", "xiaohongshu"])
    assert cleaned["collect_engine"] == "human"
    assert cleaned["channel_params"]["douyin"]["collect_engine"] == "api"
    # 不认识的值不该原样存库
    assert cleaned["channel_params"]["kuaishou"]["collect_engine"] == "hybrid"
    # 旧值存库时就顺手迁移掉
    assert cleaned["channel_params"]["xiaohongshu"]["collect_engine"] == "human"


def test_summarize_tells_frontend_which_channels_can_choose(cfg):
    rows = {r["channel"]: r for r in cp.summarize(
        cfg, {"channel_params": {"douyin": {"collect_engine": "human"}}},
        ["douyin", "weibo", "ctrip"])}
    assert rows["douyin"]["collect_engine"] == "human"
    assert rows["douyin"]["supports_browser_engine"] is True
    # 微博/携程只有接口一条路，前端不该显示这个选择器
    assert rows["weibo"]["supports_browser_engine"] is False
    assert rows["ctrip"]["supports_browser_engine"] is False


def test_normalize_removes_empty_channel_params():
    cleaned = cp.normalize({"channel_params": {"douyin": {}}}, ["douyin"])
    assert "channel_params" not in cleaned


def test_normalize_tolerates_garbage():
    assert cp.normalize({"channel_params": "not-a-dict"}, ["douyin"]) == {}
    assert cp.normalize(None, ["douyin"]) == {}


def test_normalize_result_survives_resolve(cfg):
    """清洗过的 params 再喂给 resolve，结果必须和清洗前一致（幂等且无损）。"""
    params = {"channel_params": {"douyin": {"max_works": "12"}, "ctrip": {"max_comments": "99"}}}
    cleaned = cp.normalize(params, ["douyin", "ctrip"])
    assert cp.resolve(cfg, cleaned, "douyin")["max_works"] == 12
    assert cp.resolve(cfg, cleaned, "ctrip")["max_comments_per_work"] == 99
    assert cp.normalize(cleaned, ["douyin", "ctrip"]) == cleaned


# ---------------------------------------------------------------------------
# summarize：给前端展示「最终生效值」
# ---------------------------------------------------------------------------

def test_summarize_marks_which_channels_are_customized(cfg):
    params = {"max_works": 50, "channel_params": {"ctrip": {"max_comments": 200}}}
    rows = {r["channel"]: r for r in cp.summarize(cfg, params, ["douyin", "ctrip"])}

    assert rows["douyin"]["has_works"] is True
    assert rows["douyin"]["max_works"] == 50
    assert rows["douyin"]["customized"] is False

    assert rows["ctrip"]["has_works"] is False
    assert rows["ctrip"]["max_comments"] == 200
    assert rows["ctrip"]["customized"] is True


def test_allowed_keys_split_by_platform_type():
    assert "max_works" in cp.allowed_keys("douyin")
    assert "max_works" not in cp.allowed_keys("ctrip")
    assert "max_comments" in cp.allowed_keys("tongcheng")
