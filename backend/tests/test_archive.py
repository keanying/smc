"""景区档案：三个渠道的解析、幂等落库、能力差异。

这些是**纯解析 + 落库**的测试，不打真实站点。
对着线上页面的验证做不了（开发环境连不上三个平台），
所以这里盯住的是"接进来这一层"能不能顶住：字段映射、
翻页停止条件、重采不写重、以及不支持详情的渠道被正确标记。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import tables                                      # noqa: E402
from app.services import archive as arc                        # noqa: E402
from app.services.archive.base import Region                   # noqa: E402
from app.services.archive_store import ArchiveStore            # noqa: E402

TEST_DB = "smc_archive_unit"


# ---------------------------------------------------------------- 注册
def test_three_channels_are_registered():
    got = {c["channel"]: c for c in arc.available()}
    assert set(got) == {"tongcheng", "ctrip", "qunar"}
    assert [c["channel"] for c in arc.available()] == ["tongcheng", "ctrip", "qunar"], (
        "顺序就是页面上标签页的顺序，别随手改")


def test_tongcheng_declares_no_detail_page():
    """同程没有景区详情页——这个事实必须声明出来。

    声明错了的后果不是报错，是一屏"待采集"的详情永远采不出来，
    而用户会一直点重采。
    """
    got = {c["channel"]: c for c in arc.available()}
    assert got["tongcheng"]["supports_detail"] is False
    assert got["ctrip"]["supports_detail"] is True
    assert got["qunar"]["supports_detail"] is True


# ---------------------------------------------------------------- 同程解析
TC_BLOCK = (
    '<div class="scenery_list"><span class="s_info" sid="777"></span>'
    '<a class="sce_name big" title="青岩古镇">n</a>'
    '<span>[<a title="贵州旅游景点">贵州</a>][<a title="贵阳旅游景点">贵阳</a>]</span>'
    '<span class="s_level">5A</span><p>地址：贵阳市花溪区青岩镇</p></div>'
)


def test_tongcheng_row_mapping():
    from app.services.archive.tongcheng_source import parse_page

    rows = parse_page(TC_BLOCK, "贵州", "6")
    assert len(rows) == 1
    row = rows[0]
    assert row.poi_id == "777"
    assert row.poi_name == "青岩古镇"
    assert row.scenic_level == "5A"
    assert row.address == "贵阳市花溪区青岩镇"
    assert row.province == "贵州"
    assert row.region_id == "6"


def test_tongcheng_city_takes_the_last_breadcrumb():
    """面包屑是 省 > 市，取第一个会把省名当成城市名。

    现象很隐蔽：城市列全变成省名，看起来像是"数据就是这样"，
    要等到按城市筛选一条都筛不出来才会发现。
    """
    from app.services.archive.tongcheng_source import parse_page

    assert parse_page(TC_BLOCK, "贵州", "6")[0].city_name == "贵阳"


def test_tongcheng_total_pages():
    from app.services.archive.tongcheng_source import total_pages

    assert total_pages('<input name="pageNumber" value="37"/>') == 37
    assert total_pages("<html>什么都没有</html>") == 0


def test_bundled_region_map_has_every_province():
    """随包的 pid/cid 映射：34 个省级行政区，一个都不能少。

    少了哪个省，那个省的景区在档案里就永远不存在——而页面上看起来
    一切正常（清单是满的、采集是成功的），只是少了一块。
    """
    from app.services.archive.tongcheng_source import load_bundled_regions

    regions = load_bundled_regions()
    provinces = [r for r in regions if r.level == "province"]
    assert len(provinces) == 34, f"省份数不对：{len(provinces)}"
    names = {r.region_name for r in provinces}
    for must in ("北京", "广东", "贵州", "西藏", "新疆", "台湾"):
        assert must in names, f"缺 {must}"
    assert all(r.region_id.isdigit() for r in provinces), "省的 region_id 就是 pid"


def test_bundled_city_region_id_carries_both_pid_and_cid():
    """城市的 region_id 是 `pid:cid`——只给 cid 同程是不认的。

    拆错了的后果是采回一个**别的省**的列表，而且看起来完全正常。
    """
    from app.services.archive.tongcheng_source import (
        load_bundled_regions, split_region_id,
    )

    cities = [r for r in load_bundled_regions() if r.level == "city"]
    assert len(cities) > 300
    pid, cid = split_region_id(cities[0].region_id)
    assert pid.isdigit() and cid.isdigit() and cid != "0"
    assert "·" in cities[0].region_name, "城市名带上省，否则一堆同名市分不清"
    # 省级查询 cid 必须是 0，不是空
    assert split_region_id("6") == ("6", "0")


def test_bundled_map_wins_over_page_parsing():
    """随包映射优先，解析首页只是兜底。

    反过来的话，一次没验证过的解析就能盖掉一份已知正确的映射——
    解析出残缺清单，用户拿去采，采回一片空，还不知道问题出在清单上。
    """
    import inspect

    from app.services.archive.tongcheng_source import TongchengArchiveSource

    source = inspect.getsource(TongchengArchiveSource.regions)
    bundled_at = source.index("load_bundled_regions")
    parse_at = source.index("parse_provinces")
    assert bundled_at < parse_at, "随包映射必须先试"


def test_tongcheng_province_patterns():
    from app.services.archive.tongcheng_source import parse_provinces

    found = parse_provinces('<a href="/x?pid=6">贵州</a><a data-pid="1">北京</a>')
    assert {(r.region_id, r.region_name) for r in found} == {("6", "贵州"), ("1", "北京")}


def test_tongcheng_province_parse_returns_empty_not_garbage():
    """解析不出来就返回空，让调用方给一句人话。

    返回一堆噪声更糟：用户会拿着一个乱七八糟的省份清单去采，
    采回来一片空，还不知道问题出在清单上。
    """
    from app.services.archive.tongcheng_source import parse_provinces

    assert parse_provinces("<html><body>改版了</body></html>") == []


# ---------------------------------------------------------------- 携程解析
def test_ctrip_list_reads_card_and_cardstr():
    """card 有时是对象，有时是 cardStr 里的 JSON 字符串，两种都要认。"""
    import json

    from app.services.archive.ctrip_source import parse_list_response

    rows = parse_list_response({"attractionList": [
        {"card": {"poiId": 1, "poiName": "甲", "districtId": 9,
                  "districtName": "贵阳", "address": "地址甲", "sightLevelStr": "4A"}},
        {"cardStr": json.dumps({"poiId": 2, "poiName": "乙", "districtId": 9,
                                "districtName": "贵阳"})},
        {"card": {"poiName": "没有 poiId 的脏数据"}},
    ]})
    assert [r["poi_id"] for r in rows] == ["1", "2"]
    assert rows[0]["scenic_level"] == "4A"


def test_ctrip_detail_modules():
    from app.services.archive.ctrip_source import parse_detail

    parsed = parse_detail({"templateList": [{"moduleList": [
        {"moduleName": "基础信息",
         "poiBasicModule": {"telephoneList": ["+860851-12345678", "+860851-12345678"]}},
        {"moduleName": "开放时间", "poiOpenModule": {"openTime": "08:00-18:00"}},
        {"moduleName": "优待政策", "preferentialModule": {"policyInfoList": [
            {"customDesc": "儿童",
             "policyDetail": [{"limitation": "1.2米以下", "policyDesc": "免票"}]}]}},
        {"moduleName": "图文详情",
         "introductionModule": {"introduction": "<p>山水<script>x</script>很好</p>"}},
    ]}]})
    assert parsed["tel"] == "0851-12345678", "重复的电话要去重，+86 要剥掉"
    assert parsed["open_time"] == "08:00-18:00"
    assert "1.2米以下（免票）" in parsed["discount_policy"]
    assert "山水" in parsed["scenic_intro"]
    assert "script" not in parsed["scenic_intro"], "介绍里的脚本标签必须剥干净"


def test_ctrip_nationwide_region_is_always_there():
    """携程的省份是靠猜的（接口没验证过），但全国那条是脚本里用过的。

    所以哪怕省份一条都探测不到，携程档案也必须能开采——
    否则一个没验证过的解析失灵，整个渠道就废了。
    """
    from app.services.archive.ctrip_source import NATIONWIDE_ID, parse_regions

    assert parse_regions({}) == []          # 挖不到就空手而归，不瞎编
    assert NATIONWIDE_ID == "110000"


# ---------------------------------------------------------------- 落库
@pytest.fixture()
def store():
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"
    from app.core.config import load_config, reset_cache

    reset_cache()
    cfg = load_config(use_cache=False)
    import pymysql
    conn = pymysql.connect(host=cfg.mysql["host"], port=int(cfg.mysql["port"]),
                           user=cfg.mysql["user"], password=cfg.mysql["password"],
                           autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` DEFAULT CHARSET utf8mb4")

    from app.core.db import Database
    db = Database(cfg)
    yield db
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    conn.close()
    os.environ.pop("SMC_MYSQL_DATABASE", None)
    reset_cache()


def _row(poi_id: str, **kw):
    base = {"poi_id": poi_id, "poi_name": f"景区{poi_id}", "province": "贵州",
            "city_id": "guiyang", "city_name": "贵阳", "address": "某路",
            "scenic_level": "4A", "source_url": "http://x", "region_id": "6"}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_recollect_updates_instead_of_duplicating(store):
    await store.connect()
    try:
        s = ArchiveStore(store)
        rows = [_row("1"), _row("2")]
        first = await s.upsert_many("ctrip", rows, supports_detail=True)
        again = await s.upsert_many("ctrip", rows, supports_detail=True)
        total = await store.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.SCENIC_POI_INFO}`", [], 0)
    finally:
        await store.close()

    assert first["created"] == 2
    assert again["created"] == 0 and again["updated"] == 2
    assert total == 2, "重采写重了——唯一键是 (channel, poi_id)"


@pytest.mark.asyncio
async def test_same_poi_id_on_two_channels_is_two_rows(store):
    """不同平台的 POI ID 会撞号。唯一键必须带 channel。

    不带的话，携程的 5000 和同程的 5000 会互相覆盖，
    表现是"采完同程，携程的数据莫名其妙变了"。
    """
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("ctrip", [_row("5000", poi_name="携程的")], supports_detail=True)
        await s.upsert_many("tongcheng", [_row("5000", poi_name="同程的")], supports_detail=False)
        ct = await s.get("ctrip", "5000")
        tc = await s.get("tongcheng", "5000")
    finally:
        await store.close()

    assert ct["poi_name"] == "携程的"
    assert tc["poi_name"] == "同程的"


@pytest.mark.asyncio
async def test_list_recollect_does_not_clobber_details(store):
    """列表重采**不能**把已经采到的详情冲掉。

    列表页没有电话/开放时间这些字段。把它们一起 VALUES 进去，
    每次重采名录都会把详情清空——而详情是逐条拉的，
    重新补一遍要几个小时。
    """
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("ctrip", [_row("9")], supports_detail=True)
        await s.save_detail("ctrip", "9", {
            "open_time": "08:00-18:00", "tel": "0851-1", "scenic_intro": "介绍",
            "discount_policy": "儿童免票", "amenity": "停车场"})
        await s.upsert_many("ctrip", [_row("9", poi_name="改名了")],
                            supports_detail=True)
        row = await s.get("ctrip", "9")
    finally:
        await store.close()

    assert row["poi_name"] == "改名了", "列表字段该更新的还是要更新"
    assert row["tel"] == "0851-1"
    assert row["open_time"] == "08:00-18:00"
    assert row["detail_status"] == "success", "详情状态被打回 pending 就会整批重拉"


@pytest.mark.asyncio
async def test_recollect_keeps_the_scenic_link(store):
    """重采不能把已经建立的景区关联清掉。

    用户可能几个月前就把这条建成景区了，一次例行重采就断链，
    而页面上完全看不出发生过什么。
    """
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("qunar", [_row("42")], supports_detail=True)
        await s.link_scenic("qunar", "42", "QN42")
        await s.upsert_many("qunar", [_row("42")], supports_detail=True)
        row = await s.get("qunar", "42")
    finally:
        await store.close()

    assert row["scenic_id"] == "QN42"


@pytest.mark.asyncio
async def test_channel_without_detail_is_marked_unsupported(store):
    """同程的详情状态是 unsupported，不是 pending。

    标 pending 的话，「同时采详情」会把它们全排进队列，
    逐条去请求一个根本不存在的详情页，然后全部失败——
    几千条无谓的请求，还把 IP 打热了。
    """
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("tongcheng", [_row("1")], supports_detail=False)
        await s.upsert_many("ctrip", [_row("1")], supports_detail=True)
        pending_tc = await s.pending_detail_ids("tongcheng")
        pending_ct = await s.pending_detail_ids("ctrip")
    finally:
        await store.close()

    assert pending_tc == [], "同程不该有待采详情"
    assert pending_ct == ["1"]


@pytest.mark.asyncio
async def test_search_filters(store):
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("ctrip", [
            _row("1", poi_name="青岩古镇"),
            _row("2", poi_name="天河潭", province="云南", city_name="昆明"),
        ], supports_detail=True)
        await s.link_scenic("ctrip", "1", "CT1")

        by_word = await s.search(keyword="青岩")
        by_prov = await s.search(province="云南")
        linked = await s.search(linked="yes")
        unlinked = await s.search(linked="no")
        provinces = await s.provinces("ctrip")
    finally:
        await store.close()

    assert by_word["total"] == 1 and by_word["items"][0]["poi_name"] == "青岩古镇"
    assert by_prov["total"] == 1 and by_prov["items"][0]["city_name"] == "昆明"
    assert linked["total"] == 1 and unlinked["total"] == 1
    assert {p["province"] for p in provinces} == {"贵州", "云南"}


@pytest.mark.asyncio
async def test_list_query_skips_the_long_text_columns(store):
    """列表不返回介绍/政策/设施三个 LONGTEXT。

    一页 20 条，每条介绍几千字，一次就是几百 KB——而列表上
    一个字都不显示。详情抽屉打开时再单独取那一条。
    """
    await store.connect()
    try:
        s = ArchiveStore(store)
        await s.upsert_many("ctrip", [_row("1")], supports_detail=True)
        await s.save_detail("ctrip", "1", {
            "open_time": "", "tel": "", "scenic_intro": "很长的介绍" * 500,
            "discount_policy": "", "amenity": ""})
        listed = await s.search(channel="ctrip")
        one = await s.get("ctrip", "1")
    finally:
        await store.close()

    assert "scenic_intro" not in listed["items"][0]
    assert len(one["scenic_intro"]) > 1000, "单条接口要给全"
