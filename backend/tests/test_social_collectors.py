"""抖音 / 快手 / 小红书 / 微博 四个平台采集器的解析与翻页测试。

响应结构按各平台线上实际字段名构造（字段名来源见各采集器文件头的接口清单）。
沙箱访问不到真实站点，所以这里验证的是：
    请求参数拼装 -> 分页推进 -> 字段映射 -> 多级评论层级
真实连通性由用户机器上的 scripts/selfcheck.py --sites 验证。

抖音这条用例会真的调用 Node 去算 a_bogus，等于把签名链路也一起测了。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import pytest

from app.collectors.base import CollectContext, CollectTarget
from app.core.config import load_config
from app.core.redis_client import RedisClient
from app.proxy.manager import ProxiedClient, ProxyManager


# ===================================================================
# 通用假服务器
# ===================================================================

class _StatusOnly:
    """让假服务器回一个裸状态码。小红书的 406/461/471 就是这种响应。"""

    def __init__(self, code: int):
        self.code = code


class RouteServer(BaseHTTPRequestHandler):
    """按路径返回预置响应，并记录收到的请求，供断言参数用。"""

    routes: Dict[str, Any] = {}
    received: list = []

    def _respond(self, payload: Any):
        body = (payload if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
        record = {
            "method": method,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "raw_query": parsed.query,
            "body": raw_body,
            "headers": dict(self.headers),
        }
        type(self).received.append(record)

        handler = type(self).routes.get(parsed.path)
        if handler is None:
            self._respond({"error": f"no route for {parsed.path}"})
            return
        payload = handler(record) if callable(handler) else handler
        if isinstance(payload, _StatusOnly):
            # 只回状态码，不回 JSON —— 用来模拟 406/461/471 这类验证信号
            self.send_response(payload.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._respond(payload)

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    RouteServer.routes = {}
    RouteServer.received = []
    httpd = HTTPServer(("127.0.0.1", 0), RouteServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class FakePageSession:
    """替代真实浏览器会话：提供 UA、msToken、签名 evaluate。"""

    def __init__(self, cookie="sessionid=fake; LOGIN_STATUS=1",
                 ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                 ms_token="fake-ms-token"):
        self.cookie_header = cookie
        self._ua = ua
        self._ms = ms_token
        self.account_name = "测试账号"
        self.page = None
        self.sign_calls = []

    async def start(self): ...
    async def close(self): ...
    async def user_agent(self): return self._ua
    async def local_storage(self, key): return self._ms if key == "xmst" else None

    async def evaluate(self, expression, arg=None):
        self.sign_calls.append(arg)
        return "fake_hxfalcon_sign"


async def attach_client(collector, base_url: str, headers: Dict[str, str]):
    """跳过 prepare 里的浏览器部分，直接给采集器接一个指向假服务器的客户端。"""
    config = load_config(use_cache=False)
    manager = ProxyManager(config, RedisClient(config))
    client = ProxiedClient(config, manager, collector.channel, base_headers=dict(headers))
    await client.__aenter__()
    collector._client = client
    return client


def make_ctx(**kwargs) -> CollectContext:
    defaults = dict(
        scenic_id="S100", scenic_name="西湖风景区", task_id="T-social",
        max_works=50, max_comments_per_work=50,
        max_comment_level=3, enable_sub_comments=True,
    )
    defaults.update(kwargs)
    return CollectContext(**defaults)


# ===================================================================
# 抖音
# ===================================================================

DY_AWEME = {
    "aweme_id": "7412345678901234567",
    "aweme_type": 0,
    "desc": "西湖断桥残雪真的绝了 #西湖 #杭州旅游",
    "create_time": 1786955245,
    "author": {
        "uid": "1234567890",
        "sec_uid": "MS4wLjABAAAAtestsecuid",
        "nickname": "旅行的小王",
        "avatar_thumb": {"url_list": ["https://p3.douyinpic.com/avatar.jpeg"]},
    },
    "statistics": {
        "digg_count": 12500, "collect_count": 3200,
        "comment_count": 840, "share_count": 156,
    },
    "video": {
        "play_addr": {"url_list": ["https://v3.douyinvod.com/video.mp4"]},
        "cover": {"url_list": ["https://p9.douyinpic.com/cover.jpeg"]},
        "duration": 35000,
    },
    "text_extra": [
        {"hashtag_name": "西湖"}, {"hashtag_name": "杭州旅游"}, {"type": 0},
    ],
    "ip_attribution": "浙江",
}

DY_COMMENT = {
    "cid": "7412999888777666",
    "text": "什么时候去最好？",
    "create_time": 1786961245,
    "digg_count": 88,
    "reply_comment_total": 2,
    "reply_id": "0",
    "user": {"uid": "999", "sec_uid": "MS4wLjABAAAAcommenter", "nickname": "游客A"},
    "ip_label": "江苏",
    "image_list": [{"origin_url": {"url_list": ["https://p1.douyinpic.com/c1.jpg"]}}],
}

DY_SUB_COMMENT = {
    "cid": "7412999888777999",
    "text": "十月最好",
    "create_time": 1786962245,
    "digg_count": 5,
    "reply_id": "7412999888777666",
    "user": {"uid": "888", "sec_uid": "MS4wLjABAAAAsub", "nickname": "本地人"},
    "ip_label": "浙江",
}


@pytest.mark.asyncio
async def test_douyin_search_maps_fields_and_signs(server):
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    pages = {"n": 0}

    def search_route(record):
        pages["n"] += 1
        if pages["n"] == 1:
            return {"status_code": 0, "data": [{"type": 1, "aweme_info": DY_AWEME}],
                    "has_more": 1, "extra": {"logid": "LOGID-42"}}
        return {"status_code": 0, "data": [], "has_more": 0, "extra": {}}

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = search_route

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "fake-ms-token"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx()
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1
    work = works[0]
    assert work.channel == "douyin"
    assert work.work_id == "7412345678901234567"
    assert work.work_url.endswith("/video/7412345678901234567")
    assert work.author_id == "MS4wLjABAAAAtestsecuid"
    assert work.author_name == "旅行的小王"
    assert work.likes == 12500
    assert work.collection_cnt == 3200
    assert work.comment_cnt == 840
    assert work.shares == 156
    assert work.location == "浙江"
    assert work.source_keyword == "西湖"
    assert work.label == "西湖,杭州旅游"
    assert work.publish_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-17 16:27:25"
    assert json.loads(work.video_list) == ["https://v3.douyinvod.com/video.mp4"]
    assert work.scenic_id == "S100" and work.scenic_name == "西湖风景区"

    # 请求参数：公共参数齐全
    first = RouteServer.received[0]
    query = first["query"]
    assert query["keyword"] == ["西湖"]
    assert query["device_platform"] == ["webapp"]
    assert query["aid"] == ["6383"]
    assert query["msToken"] == ["fake-ms-token"]
    assert len(query["webid"][0]) == 19
    # MediaCrawler 对齐的固定值
    assert query["from_group_id"] == ["7378810571505847586"]
    assert query["count"] == ["15"]
    assert query["list_type"] == ["multi"]

    # ⚠️ 综合搜索接口**不能带 a_bogus**
    assert "a_bogus" not in query, (
        "综合搜索带了 a_bogus。抖音不会报错，而是回 200 + 空 data，"
        "表现成「搜索第 1 页无数据」——最难查的那种失败"
    )

    # Referer 要带 aid
    assert "aid%3Df594bbd9" in first["headers"].get("Referer", "") or \
           "aid=f594bbd9" in first["headers"].get("Referer", ""), first["headers"].get("Referer")

    # 第二页要带上第一页返回的 logid 作为 search_id
    second = RouteServer.received[1]
    assert second["query"]["search_id"] == ["LOGID-42"]
    assert second["query"]["offset"] == ["15"]


@pytest.mark.asyncio
async def test_douyin_comments_two_levels(server):
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    main_calls = {"n": 0}
    sub_calls = {"n": 0}

    def comment_route(record):
        main_calls["n"] += 1
        if main_calls["n"] == 1:
            return {"status_code": 0, "comments": [DY_COMMENT], "has_more": 0, "cursor": 20}
        return {"status_code": 0, "comments": [], "has_more": 0}

    def sub_route(record):
        sub_calls["n"] += 1
        if sub_calls["n"] == 1:
            return {"status_code": 0, "comments": [DY_SUB_COMMENT], "has_more": 0, "cursor": 20}
        return {"status_code": 0, "comments": [], "has_more": 0}

    RouteServer.routes["/aweme/v1/web/comment/list/"] = comment_route
    RouteServer.routes["/aweme/v1/web/comment/list/reply/"] = sub_route

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "tok"
    await attach_client(collector, base, {})

    ctx = make_ctx()
    work = collector._to_work(ctx, DY_AWEME)
    try:
        comments = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    assert len(comments) == 2
    top, sub = comments
    assert top.comment_level == "level_1"
    assert top.comment_id == "7412999888777666"
    assert top.commenter_name == "游客A"
    assert top.likes == 88
    assert top.location == "江苏"
    assert top.sub_comment_count == 2
    assert top.comment_parent_id == ""     # reply_id 为 "0" 时要清空
    assert json.loads(top.image_list) == ["https://p1.douyinpic.com/c1.jpg"]

    assert sub.comment_level == "level_2"
    assert sub.comment_parent_id == "7412999888777666"
    assert sub.root_comment_id == "7412999888777666"
    assert sub.commenter_name == "本地人"

    # 子评论请求要带上 comment_id 与 item_id，且走 sign_reply
    sub_req = [r for r in RouteServer.received if r["path"].endswith("/reply/")][0]
    assert sub_req["query"]["comment_id"] == ["7412999888777666"]
    assert sub_req["query"]["item_id"] == ["7412345678901234567"]


# ===================================================================
# 快手
# ===================================================================

KS_FEED = {
    "type": 1,
    "photo": {
        "id": "3x3zxz4mjrsc8ke",
        "caption": "#西湖# 秋天的苏堤太美了",
        "timestamp": 1786955245000,
        "realLikeCount": 4300,
        "commentCount": 210,
        "shareCount": 33,
        "viewCount": 98000,
        "coverUrl": "https://tx2.a.kwimgs.com/cover.jpg",
        "photoUrl": "https://txmov2.a.kwimgs.com/video.mp4",
        "duration": 21000,
    },
    "author": {"id": "3x84qugg4ch9zhs", "name": "杭州小李",
               "headerUrl": "https://p2.a.yximgs.com/head.jpg"},
}

KS_COMMENT = {
    "comment_id": 55667788,
    "content": "苏堤几点人少？",
    "timestamp": 1786961245000,
    "author_id": "3xcommenter",
    "author_name": "路人甲",
    "likedCount": 12,
    "subCommentCount": 1,
    "hasSubComments": True,
    "headurl": "https://p2.a.yximgs.com/c.jpg",
}

KS_SUB = {
    "comment_id": 55667799,
    "content": "早上七点前",
    "timestamp": 1786962245000,
    "author_id": "3xsub",
    "author_name": "本地导游",
    "likedCount": 3,
}


@pytest.mark.asyncio
async def test_kuaishou_search_signs_and_paginates(server):
    httpd, base = server
    from app.collectors.kuaishou import KuaishouCollector

    calls = {"n": 0}

    def search_route(record):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": 1, "feeds": [KS_FEED], "pcursor": "2",
                    "searchSessionId": "SESS-9"}
        return {"result": 1, "feeds": [], "pcursor": "no_more"}

    RouteServer.routes["/rest/v/search/feed"] = search_route

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    session = FakePageSession()
    collector._session = session
    await attach_client(collector, base, {"Cookie": "kuaishou.server.web_st=fake"})

    ctx = make_ctx()
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1
    work = works[0]
    assert work.work_id == "3x3zxz4mjrsc8ke"
    assert work.author_id == "3x84qugg4ch9zhs"
    assert work.author_name == "杭州小李"
    assert work.likes == 4300
    assert work.comment_cnt == 210
    assert work.label == "西湖"
    assert work.work_url.endswith("/short-video/3x3zxz4mjrsc8ke")
    # 毫秒时间戳
    assert work.publish_time.strftime("%Y-%m-%d") == "2026-08-17"
    assert json.loads(work.video_list) == ["https://txmov2.a.kwimgs.com/video.mp4"]
    assert json.loads(work.extra_content)["view_count"] == 98000

    # 签名必须挂在 query 上，且请求体是紧凑 JSON
    req = RouteServer.received[0]
    assert req["query"]["__NS_hxfalcon"] == ["fake_hxfalcon_sign"]
    assert req["query"]["caver"] == ["2"]
    body = json.loads(req["body"])
    assert body["keyword"] == "西湖" and body["page"] == "search" and body["pcursor"] == "1"
    # 第二页要带上 searchSessionId
    assert json.loads(RouteServer.received[1]["body"])["searchSessionId"] == "SESS-9"
    # 每次请求都重新签名（签名绑定内容和时间窗口）
    assert len(session.sign_calls) == 2


@pytest.mark.asyncio
async def test_kuaishou_rate_limit_then_success(server, monkeypatch):
    """result:2 是服务端限流，应该退避后重试而不是直接失败。"""
    httpd, base = server
    from app.collectors import kuaishou as ks_module
    from app.collectors.kuaishou import KuaishouCollector

    calls = {"n": 0}

    def search_route(record):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"result": 2}
        return {"result": 1, "feeds": [KS_FEED], "pcursor": "no_more"}

    RouteServer.routes["/rest/v/search/feed"] = search_route

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ks_module.asyncio, "sleep", fake_sleep)

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    await attach_client(collector, base, {})

    try:
        works = [w async for w in collector.collect_by_keyword(make_ctx(), "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1
    assert calls["n"] == 3
    # 退避时间要递增
    assert len(slept) >= 2 and slept[1] > slept[0]


@pytest.mark.asyncio
async def test_kuaishou_unsigned_rejection_is_explicit(server):
    """result:50 是签名没过，错误信息要指向"重新登录"而不是含糊报错。"""
    httpd, base = server
    from app.collectors.kuaishou import KuaishouCollector

    RouteServer.routes["/rest/v/search/feed"] = {"result": 50}

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    await attach_client(collector, base, {})

    with pytest.raises(RuntimeError, match="result:50"):
        _ = [w async for w in collector.collect_by_keyword(make_ctx(), "西湖")]
    await collector._client.close()


@pytest.mark.asyncio
async def test_kuaishou_comments_two_levels(server):
    httpd, base = server
    from app.collectors.kuaishou import KuaishouCollector

    RouteServer.routes["/rest/v/photo/comment/list"] = {
        "result": 1, "rootCommentsV2": [KS_COMMENT], "pcursorV2": "no_more",
    }
    RouteServer.routes["/rest/v/photo/comment/sublist"] = {
        "result": 1, "subCommentsV2": [KS_SUB], "pcursorV2": "no_more",
    }

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    await attach_client(collector, base, {})

    ctx = make_ctx()
    work = collector._to_work(ctx, KS_FEED)
    try:
        comments = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    assert len(comments) == 2
    top, sub = comments
    assert top.comment_id == "55667788"
    assert top.commenter_name == "路人甲"
    assert top.likes == 12
    assert sub.comment_level == "level_2"
    assert sub.comment_parent_id == "55667788"

    # 子评论请求的 rootCommentId 必须是 int，不能是字符串
    sub_req = [r for r in RouteServer.received if r["path"].endswith("sublist")][0]
    body = json.loads(sub_req["body"])
    assert body["rootCommentId"] == 55667788
    assert isinstance(body["rootCommentId"], int)


# ===================================================================
# 小红书
# ===================================================================

XHS_NOTE = {
    "note_id": "65f1a2b3000000001203abcd",
    "type": "normal",
    "title": "西湖三日游攻略",
    "desc": "断桥、苏堤、雷峰塔一次玩遍",
    "time": 1786955245000,
    "ip_location": "浙江",
    "user": {"user_id": "5f8a1b2c00000000010", "nickname": "小红薯旅行家",
             "avatar": "https://sns-avatar.xhscdn.com/a.jpg"},
    "interact_info": {"liked_count": "2341", "collected_count": "876",
                      "comment_count": "153", "share_count": "42"},
    "image_list": [
        {"url_default": "https://sns-img.xhscdn.com/1.jpg"},
        {"url_default": "https://sns-img.xhscdn.com/2.jpg"},
    ],
    "tag_list": [
        {"name": "西湖", "type": "topic"},
        {"name": "杭州", "type": "topic"},
        {"name": "无关", "type": "other"},
    ],
}

XHS_COMMENT = {
    "id": "65f2c3d4000000000e01",
    "content": "请问停车方便吗",
    "create_time": 1786961245000,
    "like_count": "23",
    "sub_comment_count": "2",
    "ip_location": "江苏",
    "user_info": {"user_id": "5fcommenter", "nickname": "路过的小红薯",
                  "image": "https://sns-avatar.xhscdn.com/c.jpg"},
    "pictures": [{"url_default": "https://sns-img.xhscdn.com/c1.jpg"}],
    "sub_comments": [{
        "id": "65f2c3d4000000000e02",
        "content": "地铁更方便",
        "create_time": 1786962245000,
        "like_count": "4",
        "ip_location": "浙江",
        "user_info": {"user_id": "5fsub", "nickname": "本地人"},
        "target_comment": {"id": "65f2c3d4000000000e01"},
    }],
}


@pytest.mark.asyncio
async def test_xhs_search_signs_and_maps(server):
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    calls = {"n": 0}

    def search_route(record):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": True, "data": {
                "items": [
                    {"model_type": "note", "id": XHS_NOTE["note_id"],
                     "xsec_token": "XSEC-TOKEN-1", "note_card": XHS_NOTE},
                    {"model_type": "hot_query", "id": "ignore-me"},
                ],
                "has_more": True,
            }}
        return {"success": True, "data": {"items": [], "has_more": False}}

    RouteServer.routes["/api/sns/web/v1/search/notes"] = search_route

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc;web_session=xyz"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = make_ctx()
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1, "非笔记类型的搜索结果应该被过滤掉"
    work = works[0]
    assert work.work_id == "65f1a2b3000000001203abcd"
    assert work.author_id == "5f8a1b2c00000000010"
    assert work.author_name == "小红薯旅行家"
    assert work.title == "西湖三日游攻略"
    assert work.likes == 2341
    assert work.collection_cnt == 876
    assert work.comment_cnt == 153
    assert work.shares == 42
    assert work.location == "浙江"
    assert work.label == "西湖,杭州"    # 只取 type=topic
    assert json.loads(work.image_list) == [
        "https://sns-img.xhscdn.com/1.jpg", "https://sns-img.xhscdn.com/2.jpg",
    ]
    # xsec_token 必须留在 extra_content 里，采评论时要用
    assert json.loads(work.extra_content)["xsec_token"] == "XSEC-TOKEN-1"
    assert "xsec_token=XSEC-TOKEN-1" in work.work_url

    # 签名头齐全
    headers = RouteServer.received[0]["headers"]
    for name in ("X-S", "X-T", "X-S-Common", "X-B3-Traceid"):
        assert headers.get(name), f"缺少签名头 {name}"
    body = json.loads(RouteServer.received[0]["body"])
    assert body["keyword"] == "西湖" and body["page_size"] == 20
    assert body["search_id"]


@pytest.mark.asyncio
async def test_xhs_get_query_keeps_commas_unencoded(server):
    """签名是对「逗号不编码」的查询串算的，实际发出去的必须一致。"""
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    RouteServer.routes["/api/sns/web/v2/comment/page"] = {
        "success": True, "data": {"comments": [XHS_COMMENT], "has_more": False},
    }
    # 二级评论路由也要注册：没注册的话假服务器回的是 {"error": ...}，
    # 而现在 _check 会（正确地）把它当失败——以前这种响应被当成"成功但没数据"，
    # 平台真的返回错误时同样会被吞掉。
    RouteServer.routes["/api/sns/web/v2/comment/sub/page"] = {
        "success": True, "data": {"comments": [], "has_more": False},
    }

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    collector._tokens[XHS_NOTE["note_id"]] = "TOK-9"
    await attach_client(collector, base, {})

    ctx = make_ctx()
    work = collector._to_work(ctx, XHS_NOTE, XHS_NOTE["note_id"], "TOK-9")
    try:
        comments = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    raw_query = RouteServer.received[0]["raw_query"]
    assert "image_formats=jpg,webp,avif" in raw_query, \
        f"逗号被编码了，签名会对不上：{raw_query}"
    assert "%2C" not in raw_query

    # 一级 + 内联的二级
    assert len(comments) == 2
    top, sub = comments
    assert top.comment_id == "65f2c3d4000000000e01"
    assert top.likes == 23
    assert top.sub_comment_count == 2
    assert top.location == "江苏"
    assert json.loads(top.image_list) == ["https://sns-img.xhscdn.com/c1.jpg"]
    assert sub.comment_level == "level_2"
    assert sub.comment_parent_id == "65f2c3d4000000000e01"
    assert sub.commenter_name == "本地人"


@pytest.mark.asyncio
async def test_xhs_business_errors_are_actionable(server):
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    RouteServer.routes["/api/sns/web/v1/search/notes"] = {
        "success": False, "code": 300012, "msg": "网络连接异常",
    }

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {})

    with pytest.raises(RuntimeError, match="出口 IP"):
        _ = [w async for w in collector.collect_by_keyword(make_ctx(), "西湖")]
    await collector._client.close()


# ===================================================================
# 微博
# ===================================================================

WB_MBLOG = {
    "id": "5012345678901234",
    "mid": "5012345678901234",
    "text": '周末去了<a href="/n/西湖">#西湖#</a>，人真多<br>但风景是真好 '
            '<span class="url-icon"><img alt="[good]"></span>',
    "created_at": "Sun Aug 17 16:27:25 +0800 2026",
    "attitudes_count": 320,
    "comments_count": 45,
    "reposts_count": 12,
    "region_name": "发布于 浙江",
    "source": "iPhone客户端",
    "user": {"id": 1234567890, "screen_name": "杭州吃喝玩乐",
             "profile_image_url": "https://tvax1.sinaimg.cn/u.jpg"},
    "pics": [
        {"url": "https://wx1.sinaimg.cn/orj360/a.jpg",
         "large": {"url": "https://wx1.sinaimg.cn/large/a.jpg"}},
    ],
}

WB_COMMENT = {
    "id": "5012999888777",
    "text": '几点去人最少<span class="url-icon"><img alt="[疑问]"></span>',
    "created_at": "Sun Aug 17 18:00:00 +0800 2026",
    "like_count": 9,
    "total_number": 1,
    "rootid": "",
    "source": "来自江苏",
    "user": {"id": 999888, "screen_name": "问路的人"},
    "comments": [{
        "id": "5012999888999",
        "text": "工作日早上",
        "created_at": "Sun Aug 17 18:30:00 +0800 2026",
        "like_count": 2,
        "rootid": "5012999888777",
        "source": "来自浙江",
        "user": {"id": 777666, "screen_name": "本地土著"},
    }],
}


def test_weibo_strip_html():
    from app.collectors.weibo import strip_html

    assert strip_html("<a href='#'>#西湖#</a>好玩") == "#西湖#好玩"
    assert strip_html("第一行<br/>第二行") == "第一行\n第二行"
    assert strip_html(None) == ""


def test_weibo_uid_parsing():
    from app.collectors.weibo import WeiboCollector

    parse = WeiboCollector._parse_uid
    assert parse(CollectTarget(target_type="creator", value="1234567890")) == "1234567890"
    assert parse(CollectTarget(
        target_type="creator", value="https://weibo.com/u/1234567890")) == "1234567890"
    # 旧的移动端主页链接也要认，用户库里可能存着
    assert parse(CollectTarget(
        target_type="creator", value="https://m.weibo.cn/u/1234567890")) == "1234567890"


def test_douyin_sec_uid_parsing():
    from app.collectors.douyin import DouyinCollector

    parse = DouyinCollector._parse_sec_user_id
    assert parse(CollectTarget(
        target_type="creator", value="MS4wLjABAAAAtest")) == "MS4wLjABAAAAtest"
    assert parse(CollectTarget(
        target_type="creator",
        value="https://www.douyin.com/user/MS4wLjABAAAAxyz?from_tab_name=main",
    )) == "MS4wLjABAAAAxyz"


def test_kuaishou_user_id_parsing():
    from app.collectors.kuaishou import KuaishouCollector

    parse = KuaishouCollector._parse_user_id
    assert parse(CollectTarget(target_type="creator", value="3x84qugg4ch9zhs")) == "3x84qugg4ch9zhs"
    assert parse(CollectTarget(
        target_type="creator",
        value="https://www.kuaishou.com/profile/3x84qugg4ch9zhs")) == "3x84qugg4ch9zhs"


def test_xhs_user_parsing_extracts_token():
    from app.collectors.xhs import XhsCollector

    user_id, token = XhsCollector._parse_user(CollectTarget(
        target_type="creator",
        value="https://www.xiaohongshu.com/user/profile/5f8a1b2c?xsec_token=ABC123&xsec_source=pc_feed",
    ))
    assert user_id == "5f8a1b2c"
    assert token == "ABC123"


# ---------------------------------------------------------------------------
# a_bogus 的适用范围 —— 用户遇到的「搜索 0 条」就栽在这
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_douyin_comment_endpoints_still_sign(server):
    """搜索不签名，但评论接口必须签名 —— 别把范围收过头。"""
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/comment/list/"] = lambda r: {
        "status_code": 0, "comments": [DY_COMMENT], "has_more": 0, "cursor": 0,
    }

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "fake-ms-token"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx(max_comments_per_work=5, enable_sub_comments=False, max_comment_level=1)
    work = collector._to_work(ctx, DY_AWEME, source_keyword="西湖")
    try:
        _ = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    query = RouteServer.received[0]["query"]
    assert "a_bogus" in query, "评论接口没签名，抖音会直接拒"
    assert len(query["a_bogus"][0]) > 100


@pytest.mark.asyncio
async def test_douyin_empty_first_page_raises_with_clues(server):
    """第一页就空 = 出问题了，不能当成"采完了"悄悄结束。

    用户看到的原状是：
        [抖音] 关键字 [天山天池] 第 1 页无数据，结束
        关键字 [天山天池] 采集到 0 条作品
        任务完成；作品 新增 0 / 更新 0
    完全无法判断是这个词真没内容、还是被风控了、还是参数少了。
    """
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {
        "status_code": 0, "data": [], "has_more": 0, "extra": {},
    }

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = ""          # 故意缺 msToken，报错里应该点出来
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx()
    with pytest.raises(RuntimeError) as excinfo:
        try:
            _ = [w async for w in collector.collect_by_keyword(ctx, "天山天池")]
        finally:
            await collector._client.close()

    message = str(excinfo.value)
    assert "第 1 页就没有结果" in message
    assert "msToken 缺失" in message, "报错里要点出缺了什么"
    assert "代理 IP" in message, "报错里要给出下一步怎么办"


@pytest.mark.asyncio
async def test_douyin_later_empty_page_ends_quietly(server):
    """第二页开始空是正常的"采完了"，不能报错。"""
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    pages = {"n": 0}

    def search_route(record):
        pages["n"] += 1
        if pages["n"] == 1:
            return {"status_code": 0, "data": [{"type": 1, "aweme_info": DY_AWEME}],
                    "has_more": 1, "extra": {"logid": "L1"}}
        return {"status_code": 0, "data": [], "has_more": 0, "extra": {}}

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = search_route

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx()
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()
    assert len(works) == 1


@pytest.mark.asyncio
async def test_douyin_empty_body_is_reported_as_blocked(server):
    """抖音被风控时会回 200 + 空 JSON，要认出来。

    用「仅 API」模式测：这个模式的约定就是接口坏了立刻报错、不换路，
    所以能干净地断言那条风控提示。混合模式下的行为见下一个用例。
    """
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {}

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._cookie_header = "sessionid=fake"
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx()
    ctx.collect_engine = "api"
    with pytest.raises(RuntimeError, match="空响应"):
        try:
            _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
        finally:
            await collector._client.close()


async def test_douyin_hybrid_reports_both_failures(server):
    """混合模式下接口被风控会去换拟人；拟人也起不来时，错误里要同时有两条线索。

    只报拟人那条（"没有可用账号"）会把人带偏——用户会跑去加账号，
    可真正的起因是接口先被风控了，加了账号也一样采不到。
    """
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {}

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._cookie_header = "sessionid=fake"
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = make_ctx()          # 默认就是混合模式
    ctx.params.pop("browser_manager", None)     # 拟人起不来
    with pytest.raises(RuntimeError) as caught:
        try:
            _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
        finally:
            await collector._client.close()
    message = str(caught.value)
    assert "空响应" in message, "接口那条起因丢了"
    assert "拟人模式也失败" in message, "没说清拟人也没起来"


# ===================================================================
# 排序与时间范围筛选：各平台怎么把它翻译成自己的请求参数
# ===================================================================

from app.core import search_filters as _sf  # noqa: E402


def _ctx_with_filters(channel: str, params: dict, **kwargs):
    ctx = make_ctx(**kwargs)
    ctx.filters = _sf.resolve(channel, params)
    return ctx


@pytest.mark.asyncio
async def test_douyin_sends_filter_selected(server):
    """抖音把排序和时间打包进 filter_selected，并且要把 is_filter_search 置 1。"""
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {
        "status_code": 0, "data": [{"type": 1, "aweme_info": DY_AWEME}],
        "has_more": 0, "extra": {"logid": "L"},
    }

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = _ctx_with_filters("douyin", {"sort": "latest", "publish_within": "week"})
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    query = RouteServer.received[0]["query"]
    assert query["is_filter_search"] == ["1"], "用了筛选就必须把 is_filter_search 置 1"
    selected = json.loads(query["filter_selected"][0])
    assert selected["sort_type"] == "2", "latest 对应 sort_type=2"
    assert selected["publish_time"] == "7", "一周内对应 publish_time=7"


@pytest.mark.asyncio
async def test_douyin_omits_filter_when_unlimited(server):
    """没设筛选就别发这些参数，保持和默认搜索一致。"""
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {
        "status_code": 0, "data": [{"type": 1, "aweme_info": DY_AWEME}],
        "has_more": 0, "extra": {},
    }

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = _ctx_with_filters("douyin", {})
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    query = RouteServer.received[0]["query"]
    assert "filter_selected" not in query
    assert query["is_filter_search"] == ["0"]


@pytest.mark.asyncio
async def test_douyin_custom_dates_are_not_sent_to_server(server):
    """抖音只认预设档；自定义日期区间交给本地过滤，别往接口里塞它不认识的值。"""
    httpd, base = server
    from app.collectors.douyin import DouyinCollector

    RouteServer.routes["/aweme/v1/web/general/search/single/"] = lambda r: {
        "status_code": 0, "data": [{"type": 1, "aweme_info": DY_AWEME}],
        "has_more": 0, "extra": {},
    }

    config = load_config(use_cache=False)
    collector = DouyinCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    collector._user_agent = await collector._session.user_agent()
    collector._ms_token = "t"
    await attach_client(collector, base, {"Cookie": "sessionid=fake"})

    ctx = _ctx_with_filters(
        "douyin", {"start_date": "2026-08-01", "end_date": "2026-08-20"},
    )
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    query = RouteServer.received[0]["query"]
    assert "filter_selected" not in query
    # 但本地时间窗是有的，runner 会用它过滤
    assert ctx.filters.window is not None


@pytest.mark.asyncio
async def test_xhs_sends_sort_and_time_filters(server):
    """小红书两处都要写：顶层 sort + filters 数组里的两项。"""
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    RouteServer.routes["/api/sns/web/v1/search/notes"] = lambda r: {
        "success": True, "data": {"items": [
            {"model_type": "note", "id": XHS_NOTE["note_id"],
             "xsec_token": "XSEC-1", "note_card": XHS_NOTE},
        ], "has_more": False},
    }

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc;web_session=xyz"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = _ctx_with_filters(
        "xiaohongshu", {"sort": "most_like", "publish_within": "day"},
    )
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    body = json.loads(RouteServer.received[0]["body"])
    assert body["sort"] == "popularity_descending"
    by_type = {f["type"]: f["tags"] for f in body["filters"]}
    assert by_type["sort_type"] == ["popularity_descending"]
    assert by_type["filter_note_time"] == ["一天内"], "时间只认这几个中文词"


@pytest.mark.asyncio
async def test_kuaishou_does_not_invent_filter_params(server):
    """快手接口没有排序/时间参数，绝不能自己编几个塞进去。"""
    httpd, base = server
    from app.collectors.kuaishou import KuaishouCollector

    RouteServer.routes["/rest/v/search/feed"] = lambda r: {
        "result": 1, "feeds": [KS_FEED], "pcursor": "no_more", "searchSessionId": "S",
    }

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    await attach_client(collector, base, {"Cookie": "kuaishou.server.web_st=fake"})

    ctx = _ctx_with_filters(
        "kuaishou", {"sort": "latest", "start_date": "2026-08-01"},
    )
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    body = json.loads(RouteServer.received[0]["body"])
    assert set(body) <= {"keyword", "pcursor", "page", "searchSessionId"}, (
        f"给快手发了它不认识的参数：{sorted(set(body) - {'keyword','pcursor','page','searchSessionId'})}"
    )


# ---------------------------------------------------------------------------
# 微博：对齐用户自己那套跑通的采集脚本
# ---------------------------------------------------------------------------


# ===================================================================
# 微博：登录态自检（用户报的「登录完又让我登录 + 采集 0 条」）
# ===================================================================


# ===================================================================
# 小红书：搜索卡片必须补详情，否则时间筛选是假的
# ===================================================================

@pytest.mark.asyncio
async def test_xhs_search_fills_publish_time_from_detail(server):
    """搜索结果里没有 time 字段，必须回查 /feed 才拿得到发布时间。

    这条测试盯的不是"字段全不全"，而是**时间筛选到底生不生效**：
    publish_time 为 None 时，runner 的 in_window() 按设计恒返回 True
    （解析不出时间的一律放行），older_than_window() 恒返回 False，
    于是「时间范围」和「按最新排序提前停」两个功能同时静默失效——
    不报错、不告警，用户只会觉得"怎么采回来一堆很老的笔记"。
    """
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    # 搜索卡片：只有标题和互动数，**没有 time、没有 desc**，线上就是这样
    card = {
        "note_id": XHS_NOTE["note_id"],
        "display_title": "西湖三日游攻略",
        "type": "normal",
        "user": XHS_NOTE["user"],
        "interact_info": {"liked_count": "2341"},
    }
    RouteServer.routes["/api/sns/web/v1/search/notes"] = {
        "success": True,
        "data": {"items": [
            {"model_type": "note", "id": card["note_id"],
             "xsec_token": "XSEC-1", "note_card": card},
        ], "has_more": False},
    }
    RouteServer.routes["/api/sns/web/v1/feed"] = {
        "success": True,
        "data": {"items": [{"note_card": XHS_NOTE}]},
    }

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc;web_session=xyz"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = _ctx_with_filters("xiaohongshu", {})
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1
    work = works[0]
    assert work.publish_time is not None, "没补详情的话时间筛选形同虚设"
    assert work.publish_time.strftime("%Y-%m-%d") == "2026-08-17"
    # 详情里才有的字段也一并补上了
    assert "断桥" in work.description
    assert work.comment_cnt == 153
    assert work.location == "浙江"

    paths = [r["path"] for r in RouteServer.received]
    assert "/api/sns/web/v1/feed" in paths


@pytest.mark.asyncio
async def test_xhs_falls_back_to_card_when_detail_fails(server):
    """详情接口抽风时退回卡片，不能因此把整条作品丢掉。"""
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    card = {
        "note_id": XHS_NOTE["note_id"], "display_title": "西湖三日游攻略",
        "user": XHS_NOTE["user"], "interact_info": {"liked_count": "2341"},
    }
    RouteServer.routes["/api/sns/web/v1/search/notes"] = {
        "success": True,
        "data": {"items": [
            {"model_type": "note", "id": card["note_id"],
             "xsec_token": "T", "note_card": card},
        ], "has_more": False},
    }
    RouteServer.routes["/api/sns/web/v1/feed"] = {"success": False, "code": 300012}

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = _ctx_with_filters("xiaohongshu", {})
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert len(works) == 1
    # display_title 也要认，否则标题会是空的
    assert works[0].title == "西湖三日游攻略"


@pytest.mark.asyncio
async def test_xhs_search_carries_x_rap_param(server):
    """搜索接口要带 x-rap-param。

    RedCrack 的 XRAP_ENCRYPT_URL 名单和 xhshow 的 sign_headers(x_rap=...)
    互相印证：search/notes、feed、user_posted 都在名单里。
    少这个头搜索会直接失败，而且报错里看不出是签名少了东西。
    """
    httpd, base = server
    from app.collectors.xhs import XhsCollector

    RouteServer.routes["/api/sns/web/v1/search/notes"] = {
        "success": True, "data": {"items": [], "has_more": False},
    }

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = _ctx_with_filters("xiaohongshu", {})
    try:
        with pytest.raises(RuntimeError):     # 第一页就空，会报"没有结果"
            _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    headers = {k.lower(): v for k, v in RouteServer.received[0]["headers"].items()}
    assert "x-rap-param" in headers, f"少了 x-rap-param：{sorted(headers)}"
    assert headers.get("x-s"), "x-s 也得在"


@pytest.mark.asyncio
async def test_xhs_translates_verification_status_codes(server):
    """406/461/471 是小红书仅有的明确信号，不能糊成通用 HTTP 错。

    ⚠️ 461/471 抛的是 `LoginRequired` 而不是普通 RuntimeError，这是
    **故意的契约变更**：这两个码意味着"平台要真人来一下"（滑块/验证），
    只有 LoginRequired 才会走到 runner 的 `_pause_for_human` → 分类 →
    飞书通知那条链上去。以前它们只是个 RuntimeError，于是采集静悄悄
    地断在那儿，人根本不知道要去点验证码。406 仍然是普通错误——
    那个是签名/参数问题，喊人来也没用。
    """
    httpd, base = server
    from app.collectors.base import LoginRequired
    from app.collectors.xhs import XhsCollector

    RouteServer.routes["/api/sns/web/v1/search/notes"] = _StatusOnly(471)

    config = load_config(use_cache=False)
    collector = XhsCollector(config, ProxyManager(config, RedisClient(config)))
    collector.api_host = base
    collector._cookie = "a1=abc"
    from xhshow import Xhshow
    collector._signer = Xhshow()
    await attach_client(collector, base, {"Cookie": collector._cookie})

    ctx = _ctx_with_filters("xiaohongshu", {})
    try:
        with pytest.raises(LoginRequired) as excinfo:
            _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    message = str(excinfo.value)
    assert "验证" in message, message
    assert "471" in message


@pytest.mark.asyncio
async def test_kuaishou_creator_collection_does_not_crash(server):
    """主页采集不能一上来就 NameError。

    这条测试的由来：`collect_by_creator` 里有一段从 `collect_by_keyword`
    误粘过来的 `if page == 1:`，而那个函数里根本没有 `page` 变量——
    主页采集**第一次翻页就必崩**，而且崩的是 NameError，
    报错里完全看不出和"复制粘贴"有关。
    纯语法层面的检查（import、类型检查）也抓不到它，只有真跑一遍才会暴露。
    """
    httpd, base = server
    from app.collectors.base import CollectTarget
    from app.collectors.kuaishou import KuaishouCollector

    calls = {"n": 0}

    def feed_route(record):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": 1, "feeds": [KS_FEED], "pcursor": "2"}
        return {"result": 1, "feeds": [], "pcursor": "no_more"}

    RouteServer.routes["/rest/v/profile/feed"] = feed_route

    config = load_config(use_cache=False)
    collector = KuaishouCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector._session = FakePageSession()
    await attach_client(collector, base, {"Cookie": "kuaishou.server.web_st=fake"})

    target = CollectTarget(target_type="creator", value="3x84qugg4ch9zhs", name="杭州小李")
    ctx = make_ctx()
    try:
        works = [w async for w in collector.collect_by_creator(ctx, target)]
    finally:
        await collector._client.close()

    assert len(works) == 1
    assert works[0].work_id == "3x3zxz4mjrsc8ke"
    body = json.loads(RouteServer.received[0]["body"])
    assert body["user_id"] == "3x84qugg4ch9zhs"
    assert body["page"] == "profile"



# ===================================================================
# 微博（PC 网页端）
# ===================================================================
#
# 全部照用户那套跑通的 weibo_crawler.py 的 pc 分支：
#   搜索  s.weibo.com/weibo|realtime  → HTML，抠 mid
#   详情  weibo.com/ajax/statuses/show
#   评论  weibo.com/ajax/statuses/buildComments（max_id 游标）

WB_PC_MBLOG = {
    "id": 5012345678901234,
    "mid": "5012345678901234",
    "text": '周末去了<a href="/n/西湖">#西湖#</a>，人真多...<span>全文</span>',
    # ⚠️ longTextContent 优先级最高：只读 text 的话长微博会被截成「…全文」
    "longTextContent": "周末去了#西湖#，人真多<br>但风景是真好",
    "text_raw": "周末去了#西湖#，人真多",
    "created_at": "Sun Aug 17 16:27:25 +0800 2026",
    "region_name": "发布于 浙江",
    "attitudes_count": 320,
    "comments_count": 45,
    "reposts_count": 12,
    "source": "iPhone客户端",
    "isLongText": True,
    "user": {"id": 1234567890, "screen_name": "杭州吃喝玩乐",
             "avatar_hd": "https://tva1.sinaimg.cn/a.jpg", "gender": "f"},
    "pic_ids": ["pid1", "pid2"],
    "pic_infos": {
        "pid1": {"original": {"url": "https://wx1.sinaimg.cn/original/pid1.jpg"},
                 "large": {"url": "https://wx1.sinaimg.cn/large/pid1.jpg"}},
        # 没有 original 时降级到 largest
        "pid2": {"largest": {"url": "https://wx2.sinaimg.cn/largest/pid2.jpg"}},
    },
}

WB_PC_COMMENT = {
    "id": 5012999888777,
    "text_raw": "几点去人最少",
    "created_at": "Sun Aug 17 18:00:00 +0800 2026",
    "like_counts": 8,
    "total_number": 2,
    "source": "来自江苏",
    "user": {"id": 999888, "screen_name": "路过的网友"},
}

WB_SEARCH_HTML = """
<html><body>
  <div class="card-wrap" mid="5012345678901234"><p>正文一</p></div>
  <div class="card-wrap" mid="5012345678909999"><p>正文二</p></div>
</body></html>
"""


def _weibo_collector(base):
    """把采集器的两个 host 都指到假服务器上。"""
    from app.collectors.weibo import WeiboCollector

    config = load_config(use_cache=False)
    collector = WeiboCollector(config, ProxyManager(config, RedisClient(config)))
    collector.host = base
    collector.search_host = base
    return collector


@pytest.mark.asyncio
async def test_weibo_pc_search_extracts_mids_then_fetches_detail(server):
    """搜索页返回的是 HTML，要先抠出 mid 再逐条取详情。"""
    httpd, base = server

    RouteServer.routes["/weibo"] = lambda r: WB_SEARCH_HTML
    RouteServer.routes["/ajax/statuses/show"] = lambda r: (
        WB_PC_MBLOG if r["query"].get("id") == ["5012345678901234"]
        else {**WB_PC_MBLOG, "id": 5012345678909999, "mid": "5012345678909999"}
    )

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    ctx = _ctx_with_filters("weibo", {})
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert [w.work_id for w in works] == ["5012345678901234", "5012345678909999"]
    work = works[0]
    assert work.author_id == "1234567890"
    assert work.author_name == "杭州吃喝玩乐"
    assert work.likes == 320 and work.comment_cnt == 45 and work.shares == 12
    # longTextContent 优先，长微博不能被截成「…全文」
    assert "但风景是真好" in work.description
    assert "全文" not in work.description
    assert work.label == "西湖"
    assert work.location == "浙江"
    assert work.publish_time.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-17 16:27:25"
    # pic_infos：original 优先，没有就降级到 largest
    assert json.loads(work.image_list) == [
        "https://wx1.sinaimg.cn/original/pid1.jpg",
        "https://wx2.sinaimg.cn/largest/pid2.jpg",
    ]

    # 搜索参数照参考实现
    query = RouteServer.received[0]["query"]
    assert query["q"] == ["西湖"] and query["page"] == ["1"]
    assert query["rd"] == ["weibo"] and query["Refer"] == ["weibo_weibo"]
    assert "xsort" not in query, "综合排序不带 xsort"


@pytest.mark.asyncio
async def test_weibo_never_sends_timescope(server):
    """⚠️ 搜索请求里**不能**有 timescope。

    曾经把它塞进移动端的 containerid，理由是"移动端就是把桌面版的查询串
    搬过来的"——那是推断，没有任何参考实现这么做。代价是
    「宝珠洞索道」这类小众词直接搜不到内容。
    时间范围一律靠客户端时间窗兜底过滤。
    """
    httpd, base = server
    RouteServer.routes["/realtime"] = lambda r: WB_SEARCH_HTML
    RouteServer.routes["/ajax/statuses/show"] = lambda r: WB_PC_MBLOG

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    ctx = _ctx_with_filters("weibo", {
        "sort": "latest", "start_date": "2026-08-01", "end_date": "2026-08-27",
    })
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "八大处缆车")]
    finally:
        await collector._client.close()

    query = RouteServer.received[0]["query"]
    assert "timescope" not in query, query
    assert RouteServer.received[0]["path"] == "/realtime", "最新发布走 realtime 路径"


@pytest.mark.asyncio
async def test_weibo_hot_sort_adds_xsort(server):
    httpd, base = server
    RouteServer.routes["/weibo"] = lambda r: WB_SEARCH_HTML
    RouteServer.routes["/ajax/statuses/show"] = lambda r: WB_PC_MBLOG

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=x"})
    ctx = _ctx_with_filters("weibo", {"sort": "most_like"})
    try:
        _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()

    assert RouteServer.received[0]["query"]["xsort"] == ["hot"]


@pytest.mark.parametrize("html,expected", [
    ('<div mid="123"></div><div mid="456"></div>', ["123", "456"]),
    ('<a href="/detail/789">x</a>', ["789"]),
    ('<p action-data="cid=1&mid=555&x=2">x</p>', ["555"]),
    # 同一条出现多次只算一条，且保持页面顺序
    ('<div mid="1"></div><div mid="2"></div><div mid="1"></div>', ["1", "2"]),
    ("<html>什么都没有</html>", []),
])
def test_weibo_mid_extraction_handles_three_layouts(html, expected):
    """三种 mid 写法都要认——不同卡片模板用的不一样，只认第一种会漏掉一部分。"""
    from app.collectors.weibo import WeiboCollector

    assert WeiboCollector._extract_mids(html) == expected


@pytest.mark.asyncio
async def test_weibo_pc_comments_walk_max_id_cursor(server):
    """评论用 max_id 游标翻页，返回 0/空表示到底了。"""
    httpd, base = server

    calls = {"n": 0}

    def comments(record):
        calls["n"] += 1
        if record["query"].get("fetch_level") == ["1"]:
            return {"ok": 1, "data": [
                {**WB_PC_COMMENT, "id": 5012999888778, "text_raw": "下午三点后"},
            ], "max_id": 0}
        if calls["n"] == 1:
            return {"ok": 1, "data": [WB_PC_COMMENT], "max_id": 777666}
        return {"ok": 1, "data": [], "max_id": 0}

    RouteServer.routes["/ajax/statuses/buildComments"] = comments

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    ctx = make_ctx()
    work = collector.new_work(ctx, work_id="5012345678901234", title="x")
    try:
        comments_out = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    levels = [(c.comment_level, c.content) for c in comments_out]
    assert levels == [("level_1", "几点去人最少"), ("level_2", "下午三点后")]
    first = comments_out[0]
    assert first.likes == 8
    assert first.location == "江苏", "IP 属地在 source 里，形如「来自江苏」"
    assert first.sub_comment_count == 2

    # 一级用 flow=1/fetch_level=0，二级用 flow=0/fetch_level=1
    params = [r["query"] for r in RouteServer.received]
    assert params[0]["flow"] == ["1"] and params[0]["fetch_level"] == ["0"]
    sub = [p for p in params if p.get("fetch_level") == ["1"]][0]
    assert sub["id"] == ["5012999888777"], "二级评论的 id 是一级评论的 ID"
    # 第二页要带上游标
    second = [p for p in params if p.get("max_id") == ["777666"]]
    assert second, params


@pytest.mark.asyncio
async def test_weibo_closed_comments_do_not_raise(server):
    """评论区关了 / 作品删了：跳过这一条，不能把整批带走。"""
    httpd, base = server
    RouteServer.routes["/ajax/statuses/buildComments"] = {"ok": 0, "msg": "暂无数据"}

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    lines: list[str] = []
    ctx = make_ctx()
    ctx.log = lambda message, level="info": lines.append(f"{level}:{message}")
    work = collector.new_work(ctx, work_id="5012345678901234", title="x")
    try:
        out = [c async for c in collector.collect_comments(ctx, work)]
    finally:
        await collector._client.close()

    assert out == []
    assert any("评论不可用" in line for line in lines), lines
    # 报错里要有完整地址，能直接复制到浏览器重放
    assert any("buildComments?" in line for line in lines), lines


@pytest.mark.asyncio
async def test_weibo_visitor_cookie_is_caught_before_searching(server):
    """有 SUB 但其实是游客态时，要在搜索之前就说清楚是没登录。

    微博**给游客也发 SUB**，而且 profile 里会留着上次过期的那份，key 一个不少。
    拿这种 Cookie 去搜索不会报错，只是搜不到内容——
    采集器看到的是"没有结果"，用户以为是关键字没内容，实际是没登录。
    """
    httpd, base = server
    from app.collectors.base import LoginRequired

    # 未登录时微博把 /ajax/profile/info 重定向到登录页，返回的是 HTML
    RouteServer.routes["/ajax/profile/info"] = {"ok": 0, "msg": "请先登录"}
    RouteServer.routes["/weibo"] = lambda r: WB_SEARCH_HTML

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=visitor"})

    ctx = make_ctx()
    try:
        # 新策略：不再预判，由真实业务请求确认
        await collector._assert_logged_in(ctx)
        assert collector.login_verified is True
    finally:
        await collector._client.close()


@pytest.mark.asyncio
async def test_weibo_logged_in_check_passes_and_logs_nickname(server):
    httpd, base = server
    RouteServer.routes["/ajax/profile/info"] = {
        "ok": 1, "data": {"user": {"id": 1234567890, "screen_name": "杭州吃喝玩乐"}},
    }

    collector = _weibo_collector(base)
    await attach_client(collector, base, {"Cookie": "SUB=real; XSRF-TOKEN=tok"})

    lines: list[str] = []
    ctx = make_ctx()
    ctx.log = lambda message, level="info": lines.append(str(message))
    try:
        await collector._assert_logged_in(ctx)
    finally:
        await collector._client.close()

    assert collector.login_verified is True
    assert any("真实业务请求确认" in line for line in lines), lines


@pytest.mark.asyncio
async def test_weibo_empty_search_is_not_a_failure_once_login_is_verified(server):
    """「宝珠洞索道」这种小众词真的没内容——不该当成失败重试三轮。

    登录态已经**验证过**了，静默失败的可能性基本排除，
    剩下最可能的就是这个词确实没有相关微博。
    """
    httpd, base = server
    RouteServer.routes["/weibo"] = lambda r: "<html>抱歉，未找到相关结果</html>"

    collector = _weibo_collector(base)
    collector.login_verified = True
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    lines: list[str] = []
    ctx = _ctx_with_filters("weibo", {})
    ctx.log = lambda message, level="info": lines.append(f"{level}:{message}")
    try:
        works = [w async for w in collector.collect_by_keyword(ctx, "宝珠洞索道")]
    finally:
        await collector._client.close()

    assert works == []
    assert any(line.startswith("warn:") and "没有搜到内容" in line for line in lines), lines


@pytest.mark.asyncio
async def test_weibo_empty_search_still_raises_when_login_unverified(server):
    """没验证过登录态时，第一页就空仍然要报错。

    那种情况有可能是拿着游客 Cookie 在空跑，安静结束的话
    用户只会看到一句"采集到 0 条作品"，完全无从判断哪里出了问题。
    """
    httpd, base = server
    RouteServer.routes["/weibo"] = lambda r: "<html></html>"

    collector = _weibo_collector(base)
    collector.login_verified = False
    await attach_client(collector, base, {"Cookie": "SUB=x"})

    ctx = _ctx_with_filters("weibo", {})
    try:
        with pytest.raises(RuntimeError, match="第 1 页就没有结果"):
            _ = [w async for w in collector.collect_by_keyword(ctx, "西湖")]
    finally:
        await collector._client.close()


@pytest.mark.parametrize("mblog,expected", [
    # PC 端标准形状：pic_ids + pic_infos
    ({"pic_ids": ["a"], "pic_infos": {"a": {"original": {"url": "https://x/o.jpg"}}}},
     ["https://x/o.jpg"]),
    # 只有 pic_ids，自己拼
    ({"pic_ids": ["abc"]}, ["https://wx1.sinaimg.cn/large/abc.jpg"]),
    # 移动端形状
    ({"pics": [{"large": {"url": "https://x/l.jpg"}, "url": "https://x/t.jpg"}]},
     ["https://x/l.jpg"]),
    # ⚠️ 线上真实踩到的：元素直接是字符串，写死 p.get() 会抛 AttributeError，
    #    那个异常曾经把整个平台的采集带走
    ({"pics": ["https://x/plain.jpg"]}, ["https://x/plain.jpg"]),
    ({"pics": ["005Aa8Zbly1abc"]}, ["https://wx1.sinaimg.cn/large/005Aa8Zbly1abc"]),
    # 老接口只给缩略图
    ({"pic_urls": [{"thumbnail_pic": "https://x/thumbnail/p.jpg"}]},
     ["https://x/large/p.jpg"]),
    ({}, []),
])
def test_weibo_pic_shapes(mblog, expected):
    from app.collectors.weibo import _pic_urls
    from app.utils import normalize as nz

    assert nz.collect_urls(_pic_urls(mblog)) == expected


# ---------------------------------------------------------------------------
# 小红书搜索卡片的发布时间：corner_tag_info
# ---------------------------------------------------------------------------
def test_month_day_without_year_picks_the_right_year():
    """「08-28」这种只有月日的写法要补年份，而且不能补成未来。"""
    from datetime import datetime
    from app.utils.normalize import _parse_month_day

    # 现在是 9 月：08-28 是今年，12-31 只能是去年
    autumn = datetime(2026, 9, 4, 16, 30)
    assert _parse_month_day("08-28", now=autumn) == datetime(2026, 8, 28)
    assert _parse_month_day("12-31", now=autumn) == datetime(2025, 12, 31)
    assert _parse_month_day("09-04", now=autumn) == datetime(2026, 9, 4)

    # 现在是 3 月：08-28 还没到，只能是去年的
    spring = datetime(2026, 3, 15, 10, 0)
    assert _parse_month_day("08-28", now=spring) == datetime(2025, 8, 28)
    assert _parse_month_day("01-02", now=spring) == datetime(2026, 1, 2)

    # 不合法的宁可返回 None，也不要瞎补——publish_time 是时间窗过滤的输入
    assert _parse_month_day("13-01") is None
    assert _parse_month_day("02-30") is None


def test_xhs_search_card_publish_time_from_corner_tag():
    """搜索卡片没有 time 字段，发布时间只在 corner_tag_info 里。

    拿不到 /v1/feed 时（没触发、超时、被限流），以前 publish_time 直接是
    None，然后被 runner 的时间窗过滤整片丢掉——"搜得到却一条不入库"。
    text 的四种真实写法都取自 2026-09-04 的真实响应。
    """
    from datetime import datetime, timedelta
    from app.collectors.xhs import XhsCollector

    corner = XhsCollector._corner_publish_time
    now = datetime.now()

    hour_ago = corner({"corner_tag_info": [
        {"type": "publish_time", "text": "1小时前"}]})
    assert hour_ago is not None
    assert timedelta(minutes=50) < now - hour_ago < timedelta(minutes=70)

    two_days = corner({"corner_tag_info": [
        {"type": "publish_time", "text": "2天前"}]})
    # 用区间比而不是 .days == 2：相差正好两天再少几微秒时 .days 会是 1
    assert two_days is not None
    assert timedelta(days=2) - timedelta(minutes=1) < now - two_days \
        < timedelta(days=2) + timedelta(minutes=1)

    dated = corner({"corner_tag_info": [
        {"type": "publish_time", "text": "08-28"}]})
    assert dated is not None and (dated.month, dated.day) == (8, 28)

    # 只认 type=publish_time 的那一条，别的角标不当时间用
    assert corner({"corner_tag_info": [{"type": "sponsor", "text": "广告"}]}) is None
    assert corner({}) is None


def test_xhs_work_falls_back_to_corner_tag_when_feed_has_no_time():
    """feed 缺 time 时用卡片上的角标兜底；feed 有 time 就以 feed 为准。"""
    from datetime import datetime
    from app.collectors.base import CollectContext
    from app.collectors.xhs import XhsCollector
    from app.core.config import load_config

    collector = XhsCollector.__new__(XhsCollector)
    collector.channel = "xiaohongshu"
    collector.web_host = "https://www.xiaohongshu.com"
    ctx = CollectContext(scenic_id="S1", scenic_name="八大处", task_id="T1")

    base = {
        "note_id": "n1", "type": "normal", "display_title": "八大处缆车",
        "user": {"user_id": "u1", "nickname": "深思浅悟"},
        "interact_info": {"liked_count": "1"},
    }

    # 只有角标 → 用角标
    only_corner = dict(base, corner_tag_info=[
        {"type": "publish_time", "text": "2天前"}])
    work = collector._to_work(ctx, only_corner, "n1", "T", source_keyword="八大处")
    assert work.publish_time is not None, "角标没被用上——这条会被时间窗丢掉"
    assert (datetime.now() - work.publish_time).days == 2

    # 两者都有 → 以 feed 的 time 为准（更精确，带时分秒）
    both = dict(only_corner, time=1756000000)
    work2 = collector._to_work(ctx, both, "n1", "T", source_keyword="八大处")
    assert work2.publish_time == datetime(2025, 8, 24, 9, 46, 40), work2.publish_time


# ---------------------------------------------------------------------------
# 快手拟人采集器：选择器必须来自实测，不能是通配猜测
# ---------------------------------------------------------------------------
def test_kuaishou_browser_selectors_are_from_the_real_recording():
    """钉住 2026-09-05 录制里确认过的那几个选择器。

    上一版写的是 '[class*="comment"]'、'[class*="next"]' 这种通配——
    小红书那边已经证明过：猜出来的选择器实跑全错，而且失败时看不出原因。
    这条测试不是在验"能不能跑"，是在防止有人再把它改回通配。
    """
    from app.collectors.kuaishou_browser import KuaishouBrowserCollector as K

    # 录制里点的就是封面图 img.cover-img（它的 alt 就是文案）
    assert "img.cover-img" in K.CARD_COVER_SELECTOR
    # 联播开关：span.toggle-switch，带 is-active 表示开着
    assert "autoPlay" in K.AUTOPLAY_SWITCH_SELECTOR
    assert "toggle-switch" in K.AUTOPLAY_SWITCH_SELECTOR
    # 评论按钮在右侧那一列
    assert "commentPanel" in K.COMMENT_BUTTON_SELECTOR
    # 展开回复是 span.expand，文案「查看更多回复」
    assert "span.expand" in K.EXPAND_REPLY_SELECTOR
    # 关视频是左上角的圆形叉
    assert "circle-btn" in K.CLOSE_VIDEO_SELECTOR

    # 不许再出现 [class*=...] 这种通配猜测
    import inspect
    # 只看代码行，注释里提到它是为了解释历史，不算
    code = "\n".join(l for l in inspect.getsource(K).splitlines()
                     if not l.lstrip().startswith("#"))
    assert '[class*=' not in code, (
        "又出现了 [class*=...] 通配选择器——那是猜的，实跑必错"
    )

    # 搜索地址是录制里的真实形状
    stub = K.__new__(K)
    assert "search/{keyword}" in K._search_url_template.fget(stub)

    # ✅ 2026-09-05：用户提供了三个接口的真实请求/响应，且实跑已经从
    # /rest/v/search/feed 拿到过真实作品，所以接入了。
    assert K.integrated is True, "快手已经接入，别又关回去"

    # 真实响应是**平铺**的：评论直接挂在顶层，不在 body["data"] 里。
    # 只看 data 的话接口拦到了也解析成 0 条，日志报「8 秒没有评论到货」。
    flat = {"result": 1, "commentCountV2": 305, "pcursorV2": "1",
            "rootCommentsV2": [{"comment_id": 1, "content": "x"}]}
    assert len(K._extract_comments_from_body(flat)) == 1, (
        "顶层的 rootCommentsV2 没解析出来——这是实跑时评论全丢的原因"
    )
    nested = {"result": 1, "data": {"rootComments": [{"commentId": "c1"}]}}
    assert len(K._extract_comments_from_body(nested)) == 1, "旧形状也要兼容"
    assert K._pick(flat, "commentCountV2", "commentCount") == 305
    assert K._pick(flat, "pcursorV2", "pcursor", default="") == "1"

    # 搜索必须是「打字 + 点搜索」，不是拼 URL（用户明确要求的动线）
    goto_search = inspect.getsource(K._goto_search)
    assert ".type(" in goto_search and "SEARCH_BUTTON_SELECTOR" in goto_search, (
        "搜索又变回拼 URL 了——用户要求手动输入关键字再点搜索"
    )

    # 联播开关刚打开视频时是 aria-disabled，必须先 hover 再等它解锁
    autoplay = inspect.getsource(K._disable_autoplay)
    assert "_hover_autoplay" in autoplay and "_switch_unlocked" in autoplay, (
        "关联播没先 hover/等解锁——实跑会报 element is not enabled"
    )
