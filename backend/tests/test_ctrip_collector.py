"""携程采集器：用本地假服务器回放真实响应结构，验证翻页、去重、字段映射、评论分级。

不联网，可在 CI 稳定跑。真实接口的连通性由 scripts/selfcheck.py 在用户本机验证。
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.collectors.base import CollectContext, CollectTarget
from app.collectors.ctrip import CtripCollector
from app.core.config import load_config
from app.core.redis_client import RedisClient
from app.proxy.manager import ProxyManager

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ctrip_page.json").read_text(encoding="utf-8")
)


class _CtripHandler(BaseHTTPRequestHandler):
    """模拟携程：第 1 页返回样本，第 2 页返回空，createclientid 返回 cid。"""

    pages_served = []
    full_pages = 0            # >0 时前 N 页各返回 10 条满页
    repeat_same_page = False  # True 时每页都返回同一批 ID

    @staticmethod
    def _full_page(page_index: int, repeat: bool):
        base = 0 if repeat else (page_index - 1) * 10
        return {
            "code": 200,
            "result": {
                "totalCount": 999,
                "items": [
                    {
                        "commentId": 950000 + base + i,
                        "userInfo": {"userId": f"U{base + i}", "nickName": f"用户{base + i}"},
                        "content": f"第 {page_index} 页第 {i} 条",
                        "publishTime": "/Date(1786955245000+0800)/",
                        "ipLocatedName": "浙江",
                        "usefulCount": i,
                    }
                    for i in range(10)
                ],
            },
        }

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if "createclientid" in self.path:
            self._json({"ClientID": "0987654321abcdef"})
        else:
            self._json({"code": 500, "msg": "unexpected GET"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        page_index = payload.get("arg", {}).get("pageIndex", 1)
        type(self).pages_served.append(page_index)
        # 记录关键请求头，供断言指纹/cid 是否带上
        type(self).last_headers = dict(self.headers)
        type(self).last_query = self.path

        if type(self).repeat_same_page:
            self._json(self._full_page(page_index, repeat=True))
        elif type(self).full_pages:
            if page_index <= type(self).full_pages:
                self._json(self._full_page(page_index, repeat=False))
            else:
                self._json({"code": 200, "result": {"totalCount": 999, "items": []}})
        elif page_index == 1:
            self._json(FIXTURE)
        else:
            self._json({"code": 200, "result": {"totalCount": 23, "items": []}})

    def log_message(self, *args):
        pass


@pytest.fixture()
def ctrip_server():
    _CtripHandler.pages_served = []
    server = HTTPServer(("127.0.0.1", 0), _CtripHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _make_collector(base_url: str) -> CtripCollector:
    config = load_config(use_cache=False)
    manager = ProxyManager(config, RedisClient(config))
    collector = CtripCollector(config, manager)
    collector.client_id_url = f"{base_url}/restapi/soa2/10290/createclientid"
    collector.comment_url = f"{base_url}/restapi/soa2/13444/json/getCommentCollapseList"
    return collector


@pytest.mark.asyncio
async def test_collect_poi_maps_all_fields(ctrip_server):
    collector = _make_collector(ctrip_server)
    ctx = CollectContext(
        scenic_id="S001", scenic_name="西湖", task_id="T1",
        max_comments_per_work=100, max_comment_level=3, enable_sub_comments=True,
    )
    await collector.prepare(ctx)
    try:
        comments = [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="32289", name="西湖")
        )]
    finally:
        await collector.cleanup()

    # 2 条一级 + 1 条二级回复
    assert len(comments) == 3
    levels = [c.comment_level for c in comments]
    assert levels == ["level_1", "level_2", "level_1"]

    first = comments[0]
    assert first.comment_id == "901001"
    assert first.work_id == "32289"
    assert first.scenic_id == "S001" and first.scenic_name == "西湖"
    assert first.channel == "ctrip"
    assert first.commenter_id == "U10001"
    assert first.commenter_name == "爱旅行的小李"
    assert "荷花" in first.content
    assert first.likes == 12
    assert first.location == "浙江"
    assert first.comment_parent_id == ""
    assert first.root_comment_id == "901001"
    assert first.sub_comment_count == 1
    # .NET 日期被正确解析
    assert first.publish_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-17 16:27:25"
    # 图片列表存成 JSON 数组
    assert json.loads(first.image_list) == [
        "https://dimg.ctrip.com/a.jpg", "https://dimg.ctrip.com/b.jpg"
    ]
    # 携程特有评分进 extra_content，不丢信息
    extra = json.loads(first.extra_content)
    assert extra["landscape_score"] == 5.0
    assert extra["price_quality_score"] == 4.0
    assert extra["poi_id"] == "32289"

    reply = comments[1]
    assert reply.comment_level == "level_2"
    assert reply.comment_parent_id == "901001"
    assert reply.root_comment_id == "901001"
    assert reply.commenter_name == "景区官方"

    third = comments[2]
    # "IP属地：江苏" 前缀被清掉
    assert third.location == "江苏"
    assert third.sub_comment_count == 0


@pytest.mark.asyncio
async def test_stops_when_page_not_full(ctrip_server):
    """首页只有 2 条 < pageSize 10，说明已经到底，不该再翻第 2 页。"""
    collector = _make_collector(ctrip_server)
    ctx = CollectContext(scenic_id="S001", scenic_name="西湖", max_comments_per_work=0)
    await collector.prepare(ctx)
    try:
        _ = [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="32289")
        )]
    finally:
        await collector.cleanup()
    assert _CtripHandler.pages_served == [1]


@pytest.mark.asyncio
async def test_paginates_until_empty_page(ctrip_server, monkeypatch):
    """满页时要继续翻，直到接口返回空页才停。"""
    _CtripHandler.full_pages = 3   # 第 1~3 页满页，第 4 页空
    try:
        collector = _make_collector(ctrip_server)
        ctx = CollectContext(scenic_id="S001", scenic_name="西湖",
                             max_comments_per_work=0, enable_sub_comments=False)
        await collector.prepare(ctx)
        try:
            comments = [c async for c in collector.collect_by_poi(
                ctx, CollectTarget(target_type="poi", value="32289")
            )]
        finally:
            await collector.cleanup()
        assert _CtripHandler.pages_served == [1, 2, 3, 4]
        # 每页 10 条互不重复
        assert len(comments) == 30
        assert len({c.comment_id for c in comments}) == 30
    finally:
        _CtripHandler.full_pages = 0


@pytest.mark.asyncio
async def test_deduplicates_repeated_page(ctrip_server):
    """接口把同一页反复返回时（携程偶发），必须识别为重复并停止，而不是无限累积。"""
    _CtripHandler.repeat_same_page = True
    try:
        collector = _make_collector(ctrip_server)
        ctx = CollectContext(scenic_id="S001", scenic_name="西湖",
                             max_comments_per_work=0, enable_sub_comments=False)
        await collector.prepare(ctx)
        try:
            comments = [c async for c in collector.collect_by_poi(
                ctx, CollectTarget(target_type="poi", value="32289")
            )]
        finally:
            await collector.cleanup()
        assert len(comments) == 10
        assert len(_CtripHandler.pages_served) == 2   # 第 2 页发现全重复即停
    finally:
        _CtripHandler.repeat_same_page = False


@pytest.mark.asyncio
async def test_respects_comment_limit(ctrip_server):
    collector = _make_collector(ctrip_server)
    ctx = CollectContext(scenic_id="S001", scenic_name="西湖", max_comments_per_work=2)
    await collector.prepare(ctx)
    try:
        comments = [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="32289")
        )]
    finally:
        await collector.cleanup()
    assert len(comments) == 2


@pytest.mark.asyncio
async def test_sub_comments_disabled(ctrip_server):
    collector = _make_collector(ctrip_server)
    ctx = CollectContext(
        scenic_id="S001", scenic_name="西湖",
        enable_sub_comments=False, max_comment_level=1,
    )
    await collector.prepare(ctx)
    try:
        comments = [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="32289")
        )]
    finally:
        await collector.cleanup()
    assert all(c.comment_level == "level_1" for c in comments)
    assert len(comments) == 2


@pytest.mark.asyncio
async def test_cancellation_stops_mid_run(ctrip_server):
    collector = _make_collector(ctrip_server)
    cancel = asyncio.Event()
    cancel.set()
    ctx = CollectContext(scenic_id="S001", scenic_name="西湖", cancel_event=cancel)
    await collector.prepare(ctx)
    from app.collectors.base import TaskCancelled
    try:
        with pytest.raises(TaskCancelled):
            _ = [c async for c in collector.collect_by_poi(
                ctx, CollectTarget(target_type="poi", value="32289")
            )]
    finally:
        await collector.cleanup()


def test_poi_work_is_marked_synthetic():
    config = load_config(use_cache=False)
    collector = CtripCollector(config, ProxyManager(config, RedisClient(config)))
    ctx = CollectContext(scenic_id="S001", scenic_name="西湖", task_id="T1")
    work = collector.poi_work(ctx, CollectTarget(target_type="poi", value="32289", name="西湖"))
    assert work.work_id == "32289"
    assert work.channel == "ctrip"
    assert json.loads(work.extra_content)["synthetic"] is True
    assert work.scenic_id == "S001"


def test_rejects_non_numeric_poi(ctrip_server):
    collector = _make_collector(ctrip_server)
    ctx = CollectContext(scenic_id="S001", scenic_name="西湖")

    async def _run():
        await collector.prepare(ctx)
        try:
            return [c async for c in collector.collect_by_poi(
                ctx, CollectTarget(target_type="poi", value="abc")
            )]
        finally:
            await collector.cleanup()

    # ⚠️ 别用 asyncio.get_event_loop()：Python 3.10+ 里前面的用例跑完会把
    # 事件循环关掉/清掉，这里再取就抛 "There is no current event loop"——
    # 表现是"单跑这个用例通过、整文件跑就失败"，很容易误判成被谁改坏了。
    with pytest.raises(ValueError, match="POI_ID 必须是数字"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 真实响应回归：用用户提供的线上返回体，防止字段键名再次猜错
# ---------------------------------------------------------------------------

REAL = json.loads(
    (Path(__file__).parent / "fixtures" / "ctrip_real_page.json").read_text(encoding="utf-8")
)


class _RealHandler(_CtripHandler):
    """只回放真实响应，第 2 页起为空。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        page_index = payload.get("arg", {}).get("pageIndex", 1)
        type(self).pages_served.append(page_index)
        if page_index == 1:
            self._json(type(self).payload)
        else:
            self._json({"code": 200, "result": {"totalCount": 9736, "items": []}})


@pytest.fixture()
def real_server():
    _RealHandler.pages_served = []
    _RealHandler.payload = REAL
    server = HTTPServer(("127.0.0.1", 0), _RealHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


async def _collect_real(base_url: str, **ctx_kwargs):
    collector = _make_collector(base_url)
    ctx = CollectContext(
        scenic_id="S007", scenic_name="天山天池", task_id="T9",
        max_comments_per_work=0, max_comment_level=3, enable_sub_comments=True,
        **ctx_kwargs,
    )
    await collector.prepare(ctx)
    try:
        return [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="4649", name="天山天池")
        )]
    finally:
        await collector.cleanup()


@pytest.mark.asyncio
async def test_real_response_extracts_image_src_urls(real_server):
    """images[].imageSrcUrl 是原图地址。

    之前 _URL_KEYS 里没有这个键，四张图被静默丢成空列表——这条测试就是防它回来。
    同时要确认 imageThumbUrl（180x180 缩略图）不会被当成额外的图再收一遍。
    """
    comments = await _collect_real(real_server)
    assert len(comments) == 1

    images = json.loads(comments[0].image_list)
    assert len(images) == 4, f"应取到 4 张原图，实际 {images}"
    assert all("_W_640_10000.jpg" in url for url in images), images
    assert not any("_D_180_180" in url for url in images), "缩略图不该混进来"
    assert images[0].startswith("https://dimg04.c-ctrip.com/images/0EQ6712000tn4qerf141")


@pytest.mark.asyncio
async def test_real_response_maps_core_fields(real_server):
    comment = (await _collect_real(real_server))[0]

    assert comment.comment_id == "802887065"
    assert comment.work_id == "4649"
    assert comment.channel == "ctrip"
    assert comment.scenic_id == "S007" and comment.scenic_name == "天山天池"
    # userInfo 里是 userNick，不是 nickName
    assert comment.commenter_id == "72935427"
    assert comment.commenter_name == "YoYo_1R1P9X8E"
    assert comment.location == "上海"
    assert "雪山" in comment.content
    # /Date(1787631227000+0800)/ -> 东八区本地时间；
    # 响应里的 publishTypeTag 是「2026-08-25 发布点评」，正好互相印证
    assert comment.publish_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-25 12:13:47"
    assert comment.publish_time.strftime("%Y-%m-%d") in REAL["result"]["items"][0]["publishTypeTag"]
    # videos 是空数组，不该写成 "[]"
    assert comment.video_list is None

    extra = json.loads(comment.extra_content)
    assert extra["landscape_score"] == 5.0
    assert extra["fun_score"] == 5.0
    assert extra["price_quality_score"] == 5.0
    assert extra["tourist_type"] == "家庭亲子"
    assert extra["user_member"] == "钻石贵宾"
    assert extra["from_type"] == "来自订单"
    assert extra["detail_url"].endswith("/802887065.html")
    assert extra["poi_id"] == "4649"


@pytest.mark.asyncio
async def test_reply_info_is_the_real_reply_key(real_server):
    """回复列表的真实键是 replyInfo，不是之前猜的 replyList。"""
    payload = copy.deepcopy(REAL)
    item = payload["result"]["items"][0]
    item["replyCount"] = 1
    item["replyInfo"] = [{
        "replyId": 123456,
        "content": "感谢您的评价，欢迎再来天山天池！",
        "publishTime": "/Date(1787717627000+0800)/",
        "ipLocatedName": "新疆",
        "userInfo": {"userId": 88888, "userNick": "天山天池景区"},
    }]
    _RealHandler.payload = payload

    comments = await _collect_real(real_server)
    assert [c.comment_level for c in comments] == ["level_1", "level_2"]
    assert comments[0].sub_comment_count == 1

    reply = comments[1]
    assert reply.comment_id == "123456"
    assert reply.comment_parent_id == "802887065"
    assert reply.root_comment_id == "802887065"
    assert reply.commenter_id == "88888"
    assert reply.commenter_name == "天山天池景区"
    assert reply.location == "新疆"
    assert reply.publish_time.strftime("%Y-%m-%d") == "2026-08-26"


@pytest.mark.asyncio
async def test_flat_shop_reply_is_picked_up(real_server):
    """只有一条商家回复时，携程把它平铺在 replyContent/replyTime 上。"""
    payload = copy.deepcopy(REAL)
    item = payload["result"]["items"][0]
    item["replyContent"] = "感谢支持！"
    item["replyTime"] = "/Date(1787717627000+0800)/"
    item["replyIpLocatedName"] = "新疆"
    _RealHandler.payload = payload

    comments = await _collect_real(real_server)
    assert len(comments) == 2
    reply = comments[1]
    assert reply.comment_id == "802887065_reply"
    assert reply.content == "感谢支持！"
    assert reply.location == "新疆"
    assert json.loads(reply.extra_content)["flattened"] is True


@pytest.mark.asyncio
async def test_real_response_video_list(real_server):
    """有视频时要落到 video_list。"""
    payload = copy.deepcopy(REAL)
    payload["result"]["items"][0]["videos"] = [{
        "videoId": 1,
        "videoUrl": "https://youimg1.c-ctrip.com/target/demo.mp4",
        "coverImageUrl": "https://dimg04.c-ctrip.com/images/cover.jpg",
    }]
    _RealHandler.payload = payload

    comment = (await _collect_real(real_server))[0]
    assert json.loads(comment.video_list) == [
        "https://youimg1.c-ctrip.com/target/demo.mp4"
    ]
