"""去哪儿采集器与目录服务。

解析器是**原样引入**的（vendor/qunar_crawler/），这里不重复测它——
上游自己带了 parsers 的单测。这里测的是"接进来这一层"：
字段映射对不对、翻页会不会无限循环、撞到验证页会不会硬打、
以及导入时会不会把用户勾的名字改掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collectors.base import CollectContext          # noqa: E402
from app.collectors.qunar import QunarCollector          # noqa: E402
from app.core.constants import (                         # noqa: E402
    ALL_CHANNELS, CHANNELS_POI_BASED, CHANNELS_WITH_WORKS, CHANNEL_LABELS,
)


# ---------------------------------------------------------------- 平台注册
def test_channel_is_registered_everywhere():
    """漏注册任何一处，表现都是"这个平台在页面上不存在"，而不是报错。"""
    assert "qunar" in ALL_CHANNELS
    assert CHANNEL_LABELS["qunar"] == "去哪儿"
    assert "qunar" in CHANNELS_POI_BASED
    # 去哪儿没有"作品"，只有景区点评——混进作品型平台会让
    # 关键字过滤那一套套到它头上，把点评全丢掉
    assert "qunar" not in CHANNELS_WITH_WORKS


def test_collector_is_in_the_registry():
    from app.collectors.base import CollectorRegistry
    import app.collectors  # noqa: F401  触发注册

    assert CollectorRegistry.get("qunar") is QunarCollector


def test_channel_spelling():
    """⚠️ 上游那份代码里写的是 qunaer（多了个 e）。

    统一用正确拼写，和 constants.py 开头 douying/douyin 那条先例一致。
    下游数仓真要 qunaer，改 CHANNEL_QUNAR 一处即可——
    但**不能**两边混用，那样同一批数据会被劈成两个渠道。
    """
    from app.core.constants import CHANNEL_QUNAR
    assert CHANNEL_QUNAR == "qunar"
    assert QunarCollector.channel == CHANNEL_QUNAR


# ---------------------------------------------------------------- 字段映射
def _collector():
    from app.core.config import load_config
    return QunarCollector(load_config(), SimpleNamespace())


def _ctx():
    return CollectContext(scenic_id="S1", scenic_name="天河潭", params={})


def _row(**kw):
    base = {
        "comment_id": "259136916", "content": "体验一般",
        "commenter_id": "171332093", "commenter_name": "七*冰",
        "location": "贵州", "publish_time": "2026-08-20 00:00:00",
        "image_list": '["https://img.example/a.jpg"]',
        "extra_content": json.dumps({"score": 1}),
    }
    base.update(kw)
    return base


def test_comment_mapping():
    item = _collector()._to_comment(_ctx(), _row(), "1703719381")
    assert item.channel == "qunar"
    assert item.comment_id == "259136916"
    assert item.work_id == "1703719381"
    assert item.scenic_id == "S1"
    assert item.commenter_name == "七*冰"
    assert item.comment_level == "level_1"
    assert json.loads(item.image_list) == ["https://img.example/a.jpg"]


def test_upstream_extra_is_kept():
    """上游解析出来的 score / 票种这些是去哪儿独有的，不能在映射时丢掉。"""
    item = _collector()._to_comment(_ctx(), _row(), "p1")
    extra = json.loads(item.extra_content)
    assert extra["score"] == 1
    assert extra["source"] == "qunar"


def test_source_limitations_are_recorded_on_every_row():
    """⚠️ 点赞数和回复**这个数据源拿不到**，不是"没人点赞"。

    把限制写进每一条记录：以后有人问"去哪儿的点赞怎么全是 0"，
    翻一条数据就有答案，不用去翻代码或者文档。
    """
    item = _collector()._to_comment(_ctx(), _row(), "p1")
    extra = json.loads(item.extra_content)
    assert extra["likes_unavailable"] is True
    assert extra["replies_unavailable"] is True
    assert item.likes == 0
    assert item.sub_comment_count == 0


def test_malformed_extra_does_not_crash_mapping():
    """上游的 extra_content 不是合法 JSON 时，不能把整条映射带崩。"""
    item = _collector()._to_comment(_ctx(), _row(extra_content="{不是JSON"), "p1")
    assert json.loads(item.extra_content)["source"] == "qunar"


def test_synthetic_work_is_marked():
    """合成作品要标出来，数据中心里能一键过滤掉。"""
    from app.collectors.base import CollectTarget

    work = _collector().poi_work(
        _ctx(), CollectTarget(target_type="poi", value="p9", name="天河潭"))
    assert work.work_id == "p9"
    assert json.loads(work.extra_content)["synthetic"] is True
    assert json.loads(work.extra_content)["source"] == "qunar"


# ---------------------------------------------------------------- 验证页
@pytest.mark.parametrize("html,expected", [
    ("<title>访问验证</title>", True),
    ("<title>安全验证</title>请滑动", True),
    ("<div>请输入验证码</div>", True),
    ("<title>天河潭</title><p>正常点评</p>", False),
])
def test_verification_detection(html, expected):
    assert QunarCollector._looks_like_verification(html) is expected


def test_verification_check_only_looks_at_the_head():
    """⚠️ 只看开头 2000 字。

    正常点评页的**正文里**完全可能出现"验证码"三个字（用户在吐槽别的网站），
    全文搜会把正常页面误判成验证页，现象是"明明有数据却一条都不采"。
    """
    html = "<title>天河潭</title>" + "正常内容" * 800 + "请输入验证码"
    assert QunarCollector._looks_like_verification(html) is False


# ---------------------------------------------------------------- 目录服务
def test_catalog_verification_detection_matches_collector():
    """两处判定必须一致，否则会出现"采集说被拦了、目录说没事"这种自相矛盾。"""
    from app.services.qunar_catalog import _looks_like_verification as cat_check

    for html in ("<title>访问验证</title>", "<p>正常</p>"):
        assert cat_check(html) == QunarCollector._looks_like_verification(html)


def test_vendor_parsers_are_importable():
    """vendor 目录必须能被导入——路径错了的话，采集跑到一半才 ImportError，
    而前面开客户端、发请求的功夫全白费。"""
    from app.collectors.qunar import _ensure_vendor_on_path

    _ensure_vendor_on_path()
    from qunar_crawler.parsers import parse_comment_page, parse_poi_list  # noqa: F401
