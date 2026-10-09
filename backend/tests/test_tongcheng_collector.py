"""同程采集器：验证多候选键名映射、JSONP 兼容、翻页与去重。

因为开发环境访问不到 ly.com，这里用两套不同键名风格的假响应，
确认 FIELD_MAP 的"多候选"机制在两种结构下都能正确取值——
等拿到真实响应（scripts/probe_tongcheng.py），再补一条基于真实样本的回归用例。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.collectors.base import CollectContext, CollectTarget
from app.collectors.tongcheng import TongchengCollector
from app.core.config import load_config
from app.core.redis_client import RedisClient
from app.proxy.manager import ProxyManager

# 风格 A：dpXxx 命名
STYLE_A = {
    "total": 2,
    "pageCount": 1,
    "dpList": [
        {
            "dpId": 88001,
            "dpUserName": "同程用户A",
            "memberId": "M1",
            "dpContent": "索道排队久，但风景值回票价。",
            "dpDate": "2026-07-15 10:20:00",
            "dpGrade": 4.5,
            "usefulCount": 6,
            "dpPicList": ["https://pic.ly.com/1.jpg"],
            "replyList": [
                {"replyId": 88001001, "replyContent": "感谢反馈！",
                 "replyDate": "2026-07-15 12:00:00", "replyUserName": "景区客服"}
            ],
        },
        {
            "dpId": 88002,
            "dpUserName": "同程用户B",
            "memberId": "M2",
            "dpContent": "带娃来的，很适合亲子。",
            "dpDate": "2026-07-14 09:00:00",
            "dpGrade": 5,
            "usefulCount": 1,
        },
    ],
}

# 风格 B：通用 camelCase 命名，且包在 data 里
STYLE_B = {
    "code": 0,
    "data": {
        "totalCount": 1,
        "commentList": [
            {
                "id": 99001,
                "userName": "游客C",
                "userId": "U9",
                "content": "停车场很大。",
                "createTime": 1786955245000,
                "score": 4,
                "praiseCount": 2,
                "picList": [{"url": "https://pic.ly.com/9.jpg"}],
            }
        ],
    },
}


class _LyHandler(BaseHTTPRequestHandler):
    payload = STYLE_A
    jsonp = False
    empty_after_first = True
    pages_served = []

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        page = int(query.get("page", ["1"])[0])
        type(self).pages_served.append(page)

        if page > 1 and type(self).empty_after_first:
            data = {"total": 0, "dpList": []}
        else:
            data = type(self).payload

        body = json.dumps(data, ensure_ascii=False)
        if type(self).jsonp:
            body = f"jQuery1234_5678({body});"
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


@pytest.fixture()
def ly_server():
    _LyHandler.pages_served = []
    _LyHandler.payload = STYLE_A
    _LyHandler.jsonp = False
    server = HTTPServer(("127.0.0.1", 0), _LyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/scenery/AjaxHelper/DianPingAjax.aspx"
    server.shutdown()


async def _collect(url: str, **ctx_kwargs):
    config = load_config(use_cache=False)
    collector = TongchengCollector(config, ProxyManager(config, RedisClient(config)))
    collector.dianping_url = url
    ctx_kwargs.setdefault("max_comments_per_work", 0)
    ctx = CollectContext(scenic_id="S007", scenic_name="黄山", task_id="T9", **ctx_kwargs)
    await collector.prepare(ctx)
    try:
        return [c async for c in collector.collect_by_poi(
            ctx, CollectTarget(target_type="poi", value="32289", name="黄山")
        )]
    finally:
        await collector.cleanup()


@pytest.mark.asyncio
async def test_maps_dp_style_fields(ly_server):
    comments = await _collect(ly_server)
    assert len(comments) == 3   # 2 条一级 + 1 条回复

    first = comments[0]
    assert first.comment_id == "88001"
    assert first.channel == "tongcheng"
    assert first.work_id == "32289"
    assert first.scenic_id == "S007" and first.scenic_name == "黄山"
    assert first.commenter_id == "M1"
    assert first.commenter_name == "同程用户A"
    assert "索道" in first.content
    assert first.likes == 6
    assert first.sub_comment_count == 1
    assert first.publish_time.strftime("%Y-%m-%d %H:%M") == "2026-07-15 10:20"
    assert json.loads(first.image_list) == ["https://pic.ly.com/1.jpg"]
    assert json.loads(first.extra_content)["score"] == 4.5

    reply = comments[1]
    assert reply.comment_level == "level_2"
    assert reply.comment_parent_id == "88001"
    assert reply.commenter_name == "景区客服"


@pytest.mark.asyncio
async def test_maps_alternative_key_style(ly_server):
    """换一套键名（且列表包在 data 里）也要能正确解析。"""
    _LyHandler.payload = STYLE_B
    comments = await _collect(ly_server)
    assert len(comments) == 1
    only = comments[0]
    assert only.comment_id == "99001"
    assert only.commenter_name == "游客C"
    assert only.commenter_id == "U9"
    assert only.content == "停车场很大。"
    assert only.likes == 2
    # 毫秒时间戳
    assert only.publish_time.strftime("%Y-%m-%d") == "2026-08-17"
    assert json.loads(only.image_list) == ["https://pic.ly.com/9.jpg"]


@pytest.mark.asyncio
async def test_handles_jsonp_wrapper(ly_server):
    """响应被 jQuery 回调包一层时也要能解析出来。"""
    _LyHandler.jsonp = True
    comments = await _collect(ly_server)
    assert len(comments) == 3
    assert comments[0].comment_id == "88001"


@pytest.mark.asyncio
async def test_reply_without_id_gets_stable_derived_id(ly_server):
    """回复没有独立 ID 时要派生一个稳定 ID，两次采集结果必须一致（保证幂等入库）。"""
    _LyHandler.payload = {
        "total": 1,
        "dpList": [{
            "dpId": 77001, "dpUserName": "X", "dpContent": "正文",
            "dpDate": "2026-07-01 08:00:00",
            "replyList": [{"replyContent": "商家回复内容", "replyUserName": "商家"}],
        }],
    }
    import hashlib

    first_run = await _collect(ly_server)
    second_run = await _collect(ly_server)
    reply_ids = [c.comment_id for c in first_run if c.comment_level == "level_2"]
    assert len(reply_ids) == 1
    assert reply_ids == [c.comment_id for c in second_run if c.comment_level == "level_2"]

    # 关键：ID 必须是内容的确定性函数，而不是进程内随机 hash。
    # 断言一个写死的期望值，这样即使有人改回 hash()，跨进程跑测试也会失败。
    # 派生输入含 reply_type，避免客服回复和用户追评内容相同时撞 ID。
    expected_digest = hashlib.md5("77001|cs|商家回复内容".encode("utf-8")).hexdigest()[:12]
    assert reply_ids[0] == f"77001_r{expected_digest}"


@pytest.mark.asyncio
async def test_empty_response_stops_cleanly(ly_server):
    _LyHandler.payload = {"total": 0, "dpList": []}
    comments = await _collect(ly_server)
    assert comments == []
    assert _LyHandler.pages_served == [1]


# ===================================================================
# 真实响应回归测试
# ===================================================================
# fixtures/tongcheng_page.json 是 2026-08 从线上抓下来的真实响应
# （sid=32289 八大处公园，第 1 页）。字段映射以它为准。

from pathlib import Path as _Path

_REAL_FIXTURE_PATH = _Path(__file__).parent / "fixtures" / "tongcheng_page.json"
_HAS_REAL_FIXTURE = _REAL_FIXTURE_PATH.exists()


@pytest.mark.skipif(not _HAS_REAL_FIXTURE, reason="缺少真实响应样本")
@pytest.mark.asyncio
async def test_real_response_end_to_end(ly_server):
    """用线上真实响应跑一遍完整解析，确认每个字段都落到位。"""
    real = json.loads(_REAL_FIXTURE_PATH.read_text(encoding="utf-8"))
    _LyHandler.payload = real
    _LyHandler.empty_after_first = True

    comments = await _collect(ly_server, max_comments_per_work=0)

    level1 = [c for c in comments if c.comment_level == "level_1"]
    level2 = [c for c in comments if c.comment_level == "level_2"]

    # 真实样本：10 条一级评论，其中 3 条带客服回复
    assert len(level1) == 10, f"一级评论数不对：{len(level1)}"
    assert len(level2) == 3, f"客服回复数不对：{len(level2)}"

    first = level1[0]
    assert first.channel == "tongcheng"
    assert first.work_id == "32289"
    assert first.scenic_id == "S007"
    assert first.comment_id == "6d2fa8c8-4822-4042-80f4-dfe4c347c842"
    assert first.content == "就是人太多了，人山人海的"
    assert first.commenter_name == "同程会员_C5813641E07"
    assert first.location == "北京"
    assert first.publish_time.strftime("%Y-%m-%d") == "2025-01-29"
    assert first.likes == 0

    extra = json.loads(first.extra_content)
    assert extra["sid"] == "32289"
    assert extra["rating_text"] == "好评"
    assert extra["item_name"] == "八大处公园"
    # homeId 线上恒为空，ID 退回用了用户名，来源要标出来
    assert extra["commenter_id_source"] == "dpUserName"
    assert first.commenter_id == "同程会员_C5813641E07"

    # 同一个用户的两条评论，commenter_id 必须一致（能做去重/统计）
    repeat = [c for c in level1 if c.commenter_name == "BD564A346DAED8CD"]
    assert len(repeat) == 2
    assert repeat[0].commenter_id == repeat[1].commenter_id

    # 图片：真实样本里 2 条带图，且是协议相对 URL，要补成 https
    with_images = [c for c in level1 if c.image_list]
    assert len(with_images) == 2
    urls = json.loads(with_images[0].image_list)
    assert all(u.startswith("https://") for u in urls), urls
    assert not any(u.startswith("//") for u in urls)
    # 一张图只存一个地址，不能把 imgUrl/originalImgUrl/smallImgUrl 三个都存进去
    assert len(urls) == len(set(urls))
    assert not any("_150x150_" in u for u in urls), "存成缩略图了"

    # 客服回复：没有独立 ID，要派生且标明来源
    reply = level2[0]
    assert reply.commenter_name == "同程客服"
    assert reply.comment_parent_id in {c.comment_id for c in level1}
    assert reply.comment_id.startswith(reply.comment_parent_id + "_r")
    assert json.loads(reply.extra_content)["reply_type"] == "cs"
    assert "尊敬的客户" in reply.content


@pytest.mark.skipif(not _HAS_REAL_FIXTURE, reason="缺少真实响应样本")
def test_real_response_total_and_pages():
    """总数在顶层 totalNum，总页数只在 pageInfo.totalPage 里。"""
    from app.collectors.tongcheng import TongchengCollector

    real = json.loads(_REAL_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert TongchengCollector._extract_total(real) == 740
    assert TongchengCollector._extract_page_count(real) == 74


@pytest.mark.asyncio
async def test_api_failure_is_surfaced(ly_server):
    """isSuccess 是整数 0 而不是布尔 False，不能用 is 判断。"""
    _LyHandler.payload = {"isSuccess": 0, "errorMsg": "参数错误", "dpList": []}
    with pytest.raises(RuntimeError, match="参数错误"):
        await _collect(ly_server)
