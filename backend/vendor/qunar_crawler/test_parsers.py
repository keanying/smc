from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfoNotFoundError

from qunar_crawler.crawler import QunarCrawler
from qunar_crawler.models import COMMENT_COLUMNS, POI_COLUMNS
from qunar_crawler import utils
from qunar_crawler.parsers import (
    attach_qunar_cities,
    flatten_regions,
    parse_city_index,
    parse_comment_page,
    parse_poi_detail,
    parse_poi_list,
)


def test_city_index_and_region_join() -> None:
    html = '''
    <a href="/city/guiyang" data-ce-t="guiyang">贵阳</a>
    <a href="/city/beijing" data-ce-t="beijing">北京景点</a>
    '''
    cities = parse_city_index(html)
    assert [(c.city_id, c.city_name) for c in cities] == [("guiyang", "贵阳"), ("beijing", "北京")]

    payload = [
        {"c": 52, "n": "贵州省", "ch": [{"c": "5201", "n": "贵阳市", "ch": [{"c": "520111", "n": "花溪区"}]}]}
    ]
    rows = attach_qunar_cities(flatten_regions(payload, "test"), cities)
    assert rows[0]["qunar_city_id"] == "guiyang"
    assert rows[0]["district_name"] == "花溪区"


def test_poi_list_and_detail() -> None:
    list_html = '''
    <h1>贵阳景点大全：开放时间与地址</h1>
    <a class="sg-item" href="/1703719381">
      <span class="sg-item-tag">4A 级景区</span>
      <h3>天河潭旅游度假区<span class="lv">4A</span></h3>
      <span class="sg-item-key">📍贵州省贵阳市花溪区石板镇</span>
    </a>
    <a href="/list?city=guiyang&page=101">101</a>
    '''
    page = parse_poi_list(list_html)
    assert page.city_name == "贵阳"
    assert page.total_pages == 101
    assert page.items[0].poi_id == "1703719381"
    assert page.items[0].scenic_level == "4A"

    detail_html = '''
    <nav class="sg-crumb"><a href="/city/guiyang">贵阳景点</a></nav>
    <script type="application/ld+json">{
      "@context":"https://schema.org","@type":"TouristAttraction","name":"天河潭旅游度假区",
      "description":"第一段。\\n第二段。","address":{"streetAddress":"贵州省贵阳市花溪区石板镇","addressLocality":"贵阳"},
      "telephone":"4009009995","openingHours":["03月01日-10月31日 08:30-18:00","11月01日-02月28日 09:00-17:00"]
    }</script>
    <table class="sg-policy-table"><tbody><tr><td>儿童</td><td>1.2米以下</td><td>免费</td></tr></tbody>
      <tfoot><tr><td>补充说明：须带证件</td></tr></tfoot></table>
    <span class="sg-fac-grid-name">停车场</span><span class="sg-fac-grid-name">卫生间</span>
    '''
    record = parse_poi_detail(detail_html, "1703719381", scenic_level="4A")
    assert list(record.to_dict()) == POI_COLUMNS
    assert record.open_time == "03月01日-10月31日 08:30-18:00；11月01日-02月28日 09:00-17:00"
    assert "儿童" in record.discount_policy
    assert record.amenity == "停车场；卫生间"


def test_comment_embedded_payload_and_reserved_fields() -> None:
    next_payload = 'x:{"comment":{"id":"259136916","user":"七*冰","date":"2026-08-20","score":1,"text":"体验一般","from":"贵州","headImg":"//qcommons.qunar.com/headshot/headshotsById/171332093.png?ssl=true&l","ticketName":"$undefined","dayTripPackageName":"$undefined","images":1,"imgs":[{"big":"https://img.example/a.jpg"}],"tags":[]}}'
    script = "self.__next_f.push(" + json.dumps([1, next_payload], ensure_ascii=False) + ")"
    html = f'''
    <title>天河潭旅游度假区怎么样？ - 去哪儿</title>
    <script>{script}</script>
    <script type="application/ld+json">{{"@type":"ItemList","itemListElement":[{{"item":{{"itemReviewed":{{"name":"天河潭旅游度假区"}}}}}}]}}</script>
    <a href="/1703719381/comment?pageNum=209">209</a>
    '''
    page = parse_comment_page(html, "1703719381", crawl_time="2026-09-11 19:00:00")
    assert page.total_pages == 209
    assert len(page.comments) == 1
    row = page.comments[0]
    assert list(row) == COMMENT_COLUMNS
    assert row["comment_id"] == "259136916"
    assert row["commenter_id"] == "171332093"
    assert row["channel"] == "qunaer"
    assert row["sentiment_label"] == ""
    assert row["dimension_tags"] == "[]"
    assert json.loads(row["image_list"]) == ["https://img.example/a.jpg"]
    assert json.loads(row["extra_content"])["score"] == 1


def test_comment_page_range_is_inclusive(tmp_path: Path) -> None:
    def page_html(page: int) -> str:
        payload = (
            'x:{"comment":{"id":"comment-' + str(page) + '","user":"用户",'
            '"date":"2026-08-20","score":5,"text":"第' + str(page) + '页",'
            '"from":"贵州","headImg":"","imgs":[],"tags":[]}}'
        )
        script = "self.__next_f.push(" + json.dumps([1, payload], ensure_ascii=False) + ")"
        return f'<script>{script}</script><a href="/1703719381/comment?pageNum=9">9</a>'

    class FakeClient:
        def __init__(self) -> None:
            self.pages: list[int] = []

        def get(self, url: str, *, params: dict[str, int] | None = None) -> SimpleNamespace:
            page = (params or {}).get("pageNum", 1)
            self.pages.append(page)
            return SimpleNamespace(text=page_html(page))

    client = FakeClient()
    crawler = QunarCrawler(client)  # type: ignore[arg-type]
    output = tmp_path / "comments.csv"
    count = crawler.crawl_comments(
        ["1703719381"],
        output,
        scenic_names={"1703719381": "天河潭旅游度假区"},
        start_page=2,
        end_page=3,
    )
    assert count == 2
    assert client.pages == [2, 3]


def test_china_now_falls_back_without_iana_timezone(monkeypatch) -> None:
    def missing_timezone(key: str) -> None:
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(utils, "ZoneInfo", missing_timezone)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", utils.china_now())
