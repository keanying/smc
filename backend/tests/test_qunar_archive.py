"""去哪儿档案与行政区划：页面上能不能看见、能不能重新采。

为什么单独一个文件：test_qunar.py 测的是"采集器接得对不对"，
纯解析层，不需要数据库；这里测的是**落库和页面取数**这一段，
要真的建库建表，跑得慢，混在一起会拖慢那边。

被测的两件事在此之前都是"写了没人读"：
  - src_opinion_scenic_poi_info  导入时会写，但页面上没有任何地方读它
  - flatten_regions/attach_qunar_cities  在 vendor 里，一次都没被调用过
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import tables                                  # noqa: E402
from app.services.qunar_catalog import QunarCatalog        # noqa: E402

TEST_DB = "smc_qunar_archive_test"

CITY_HTML = (
    '<a href="/city/guiyang" data-ce-t="guiyang">贵阳</a>'
    '<a href="/city/zunyi" data-ce-t="zunyi">遵义</a>'
    '<a href="/city/beijing" data-ce-t="beijing">北京</a>'
)

#: 两省三市：贵阳/遵义在贵州，北京在北京市。第三个区县故意叫"城关区"——
#: 全国有一堆同名的城关区，用来盯住"同名不唯一就不接"这条规则。
REGION_PAYLOAD = [
    {"c": "52", "n": "贵州省", "ch": [
        {"c": "5201", "n": "贵阳市", "ch": [{"c": "520111", "n": "花溪区"}]},
        {"c": "5203", "n": "遵义市", "ch": [{"c": "520302", "n": "红花岗区"}]},
    ]},
    {"c": "11", "n": "北京市", "ch": [
        {"c": "1101", "n": "北京市", "ch": [{"c": "110101", "n": "东城区"}]},
    ]},
    {"c": "62", "n": "甘肃省", "ch": [
        {"c": "6201", "n": "兰州市", "ch": [{"c": "620102", "n": "城关区"}]},
    ]},
]


@pytest.fixture()
def db_cfg():
    import os

    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"
    from app.core.config import load_config, reset_cache

    reset_cache()
    cfg = load_config(use_cache=False)
    mysql = cfg.mysql

    import pymysql
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]),
                           user=mysql["user"], password=mysql["password"],
                           autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` DEFAULT CHARSET utf8mb4")
    yield cfg
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    conn.close()
    os.environ.pop("SMC_MYSQL_DATABASE", None)
    reset_cache()


@pytest.fixture()
async def db(db_cfg):
    from app.core.db import Database

    database = Database(db_cfg)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def _catalog(db, *, city_html=CITY_HTML, payload=REGION_PAYLOAD):
    """把 HTTP 那一层换成固定响应，剩下的逻辑原样跑。"""
    cat = QunarCatalog(SimpleNamespace(), None, db)

    async def _text(url, params=None):
        return city_html

    async def _json(url):
        return payload

    cat._get_text = _text      # type: ignore[method-assign]
    cat._get_json = _json      # type: ignore[method-assign]
    return cat


# ---------------------------------------------------------------- 行政区划
@pytest.mark.asyncio
async def test_regions_sync_writes_rows_and_matches_cities(db):
    result = await _catalog(db).sync_regions("test://pca.json")

    assert result["total"] == 4, "四个区县应该摊成四行"
    assert result["saved"] == 4
    rows = await db.fetch_all(
        f"SELECT province_name, city_name, district_name, qunar_city_id "
        f"FROM `{tables.QUNAR_REGION}` ORDER BY province_code, city_code")
    got = {(r["city_name"], r["qunar_city_id"]) for r in rows}
    assert ("贵阳市", "guiyang") in got
    assert ("遵义市", "zunyi") in got
    assert ("北京市", "beijing") in got


@pytest.mark.asyncio
async def test_unmatched_region_is_left_blank_not_guessed(db):
    """兰州没有出现在去哪儿城市列表里，它那一行必须留空。

    留空是有代价的（按省筛的时候看不到兰州），但接错的代价更大：
    接错之后点"兰州"拉回来的是别的城市的景区，而且看起来完全正常。
    """
    await _catalog(db).sync_regions("test://pca.json")
    row = await db.fetch_one(
        f"SELECT qunar_city_id FROM `{tables.QUNAR_REGION}` "
        f"WHERE city_name = %s", ["兰州市"])
    assert row["qunar_city_id"] == ""


@pytest.mark.asyncio
async def test_regions_sync_is_idempotent(db):
    cat = _catalog(db)
    await cat.sync_regions("test://pca.json")
    await cat.sync_regions("test://pca.json")
    total = await db.fetch_value(
        f"SELECT COUNT(*) AS c FROM `{tables.QUNAR_REGION}`", [], 0)
    assert total == 4, "同步两次不该变成八行——唯一键是省市区三级代码"


@pytest.mark.asyncio
async def test_provinces_groups_cities_and_skips_unmatched(db):
    cat = _catalog(db)
    await cat.sync_regions("test://pca.json")
    data = await cat.provinces()

    by_name = {p["province"]: p for p in data}
    assert sorted(c["city_id"] for c in by_name["贵州省"]["cities"]) == ["guiyang", "zunyi"]
    assert "甘肃省" not in by_name, (
        "兰州接不上去哪儿，甘肃省里一个可用城市都没有——"
        "列出来只会让人点了没反应")


@pytest.mark.asyncio
async def test_province_of_returns_empty_before_sync(db):
    """没同步过行政区划时必须返回空串而不是报错。

    导入接口每次都会调它。之前没有这张表，用户升级上来第一次导入时
    表是空的——这里要是抛异常，整个导入就废了。
    """
    assert await _catalog(db).province_of("guiyang") == ""


@pytest.mark.asyncio
async def test_province_of_finds_the_province(db):
    cat = _catalog(db)
    await cat.sync_regions("test://pca.json")
    assert await cat.province_of("guiyang") == "贵州省"
    assert await cat.province_of("nowhere") == ""


# ---------------------------------------------------------------- 档案
@pytest.mark.asyncio
async def test_archive_saves_and_reads_back(db):
    cat = QunarCatalog(SimpleNamespace(), None, db)
    await cat.save_poi_info({
        "poi_id": "1703719381", "poi_name": "天河潭旅游度假区",
        "city_id": "guiyang", "city_name": "贵阳",
        "address": "贵阳市花溪区", "tel": "0851-83320666",
        "open_time": "09:00-17:00", "scenic_level": "4A",
        "discount_policy": "儿童免票", "amenity": "停车场",
        "source_url": "https://sight.qunar.com/1703719381",
    }, scenic_id="QN1703719381")

    page = await cat.list_saved(scenic_id="QN1703719381", page_size=1)
    item = page["items"][0]
    assert item["tel"] == "0851-83320666"
    assert item["discount_policy"] == "儿童免票"
    assert item["crawl_time"] is not None, "没有采集时间的话页面上没法说'上次采集'"


@pytest.mark.asyncio
async def test_refresh_overwrites_but_keeps_the_scenic_link(db):
    """重新采集是**全量覆盖**——用户明确点了刷新，就是要页面上的新值。

    唯一不能动的是 scenic_id：刷新时不带它（因为刷的是档案不是关联），
    一起覆盖的话景区详情页立刻就查不到这条档案了。
    """
    cat = QunarCatalog(SimpleNamespace(), None, db)
    base = {"poi_id": "888", "poi_name": "青岩古镇", "city_id": "guiyang",
            "city_name": "贵阳", "tel": "旧电话", "open_time": "08:00-18:00"}
    await cat.save_poi_info(base, scenic_id="QN888")
    await cat.save_poi_info({**base, "tel": "新电话", "open_time": "07:30-19:00"})

    item = (await cat.list_saved(scenic_id="QN888", page_size=1))["items"][0]
    assert item["tel"] == "新电话"
    assert item["open_time"] == "07:30-19:00"
    assert item["scenic_id"] == "QN888", "刷新把景区关联清掉了，详情页会变成'还没采过'"
