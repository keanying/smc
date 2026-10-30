# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""景区盒子 ↔ 主：

- 景区版不开放的接口一律拦；
- 主登记盒子、发令牌、分景区；盒子拿令牌拉分配、上报作品 / 评论；
- 盒子只能报、只能看、只能改分给它的景区（别的景区的数据查不到、改不了）；
- 盒子定时同步分配：景区、关键字跟着主变，不再分的停用；
- 盒子的客户端：日期时间来回不变形、连不上主会重试、主拒绝时报清楚原因。

主用一个真实应用 + 独立测试库；盒子那边的同步在同一个应用的事件循环里跑（portal），
省得起两个 TestClient（数据库连接池绑在第一个的循环上）。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx
import pytest

from app import edition as E
from app.box.codec import decode, encode
from app.collectors.base import CommentItem, WorkItem

TEST_DB = "scenic_media_edition_test"


def test_denied_rules():
    deny = lambda m, p: E.denied_reason("scenic", m, p)  # noqa: E731
    assert E.denied_reason("full", "DELETE", "/api/scenics/x") == ""
    assert deny("GET", "/api/scenics") == ""
    for method, path in (("POST", "/api/scenics"), ("DELETE", "/api/scenics/s1"),
                         ("POST", "/api/archive/collect"), ("PUT", "/api/cluster/nodes/n1/accept"),
                         ("POST", "/api/metrics/runs"), ("POST", "/api/qunar/import"),
                         ("POST", "/api/settings/notify/test"), ("GET", "/api/boxes"),
                         ("POST", "/api/box/data/save_works")):
        assert deny(method, path), path
    assert deny("PUT", "/api/settings") == ""
    assert E.settings_allowed("scenic", "crawl", {"account_quota": {}})
    assert not E.settings_allowed("scenic", "proxy", {"enabled": True})
    for method, path in (("POST", "/api/tasks"), ("POST", "/api/accounts"),
                         ("PUT", "/api/labeling/comments/3"), ("POST", "/api/edition/sync")):
        assert deny(method, path) == "", path


def test_codec_round_trip():
    when = datetime(2026, 10, 30, 9, 30, 15)
    work = WorkItem(channel="douyin", work_id="w1", scenic_id="s1", publish_time=when, likes=3)
    back = decode(encode({"items": [work], "since": when, "keys": {"a", "b"}}))
    assert back["since"] == when and sorted(back["keys"]) == ["a", "b"]
    assert back["items"][0]["publish_time"] == when and back["items"][0]["likes"] == 3


def test_client_retries_then_reports_clearly():
    from app.box.client import MasterClient, MasterUnavailable
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"code": 503, "message": "忙"})
        assert request.headers["x-smc-box-token"] == "t"
        return httpx.Response(200, json={"code": 0, "data": encode([3, 1])})

    client = MasterClient("http://master", "t", retry_delays=(0, 0, 0), transport=httpx.MockTransport(flaky))
    assert asyncio.run(client.request("POST", "/api/box/data/save_works", json={})) == [3, 1]
    assert calls["n"] == 3

    refuse = httpx.MockTransport(lambda r: httpx.Response(403, json={"code": 403, "message": "景区 x 没有分给这个盒子"}))
    client = MasterClient("http://master", "t", retry_delays=(0, 0), transport=refuse)
    with pytest.raises(MasterUnavailable, match="没有分给这个盒子"):
        asyncio.run(client.request("GET", "/api/box/assignment"))

    down = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    client = MasterClient("http://master", "t", retry_delays=(0,), transport=down)
    with pytest.raises(MasterUnavailable, match="连不上主"):
        asyncio.run(client.request("GET", "/api/box/assignment"))
    with pytest.raises(MasterUnavailable, match="还没配"):
        asyncio.run(MasterClient("", "").request("GET", "/x"))


@pytest.fixture(scope="module")
def app_client():
    saved = {k: os.environ.get(k) for k in ("SMC_MYSQL_DATABASE", "SMC_SCHEDULER_ENABLED")}
    os.environ.update({"SMC_MYSQL_DATABASE": TEST_DB, "SMC_SCHEDULER_ENABLED": "false"})
    from app.core.config import load_config, reset_cache
    reset_cache()
    from fastapi.testclient import TestClient
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        E._current = "full"
        import pymysql
        cfg = load_config(use_cache=False)
        try:
            conn = pymysql.connect(host=cfg.get("mysql.host"), port=int(cfg.get("mysql.port")),
                                   user=cfg.get("mysql.user"), password=cfg.get("mysql.password"))
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cache()


def _ok(r):
    body = r.json()
    assert r.status_code == 200 and body.get("code", 0) == 0, body
    return body.get("data", body)


def _work(scenic_id, work_id, **kw):
    return WorkItem(channel="douyin", work_id=work_id, scenic_id=scenic_id, scenic_name=scenic_id,
                    title=f"{scenic_id} 的作品", crawl_time=datetime.now(), **kw).finalize()


def _comment(scenic_id, work_id, comment_id, content="排队太久了"):
    return CommentItem(channel="douyin", comment_id=comment_id, work_id=work_id, scenic_id=scenic_id,
                       scenic_name=scenic_id, content=content, publish_time=datetime(2026, 10, 29, 12),
                       crawl_time=datetime.now()).finalize()


def test_master_and_box(app_client):
    c = app_client
    E._current = "full"
    for sid, name in (("lingshan", "灵山胜境"), ("yuantouzhu", "鼋头渚")):
        _ok(c.post("/api/scenics", json={"scenic_id": sid, "scenic_name": name, "city": "无锡"}))
        _ok(c.post(f"/api/scenics/{sid}/keywords", json={"keywords": [name]}))
    _ok(c.post("/api/scenics/lingshan/targets", json={
        "channel": "ctrip", "target_type": "poi", "target_id": "123", "target_name": "灵山"}))

    # ---- 主：登记两个盒子，各分一个景区
    a = _ok(c.post("/api/boxes", json={"box_name": "灵山 1号盒子", "scenic_ids": ["lingshan"]}))
    b = _ok(c.post("/api/boxes", json={"box_name": "鼋头渚 1号盒子", "scenic_ids": ["yuantouzhu"]}))
    ha, hb = {"X-SMC-Box-Token": a["token"]}, {"X-SMC-Box-Token": b["token"]}
    assert c.post("/api/boxes", json={"box_name": "x", "scenic_ids": ["nope"]}).status_code == 400

    # 令牌不对 / 盒子调不该调的接口
    assert c.get("/api/box/assignment", headers={"X-SMC-Box-Token": "bad"}).status_code == 401
    assert c.get("/api/box/assignment").status_code == 401
    assert c.get("/api/scenics", headers=ha).status_code == 403
    assert c.get("/api/boxes", headers=ha).status_code == 403

    got = _ok(c.get("/api/box/assignment", headers=ha))
    assert got["box_name"] == "灵山 1号盒子"
    assert [s["scenic_id"] for s in got["bundle"]["scenics"]] == ["lingshan"]
    assert got["bundle"]["scenics"][0]["targets"][0]["target_id"] == "123"

    # ---- 盒子上报：只能报自己景区的
    r = c.post("/api/box/data/save_works", headers=ha,
               json=encode({"items": [_work("yuantouzhu", "w9")]}))
    assert r.status_code == 403 and "没有分给这个盒子" in r.json()["message"]
    assert decode(_ok(c.post("/api/box/data/save_works", headers=ha,
                             json=encode({"items": [_work("lingshan", "w1"), _work("lingshan", "w2")]})))) == [2, 0]
    assert decode(_ok(c.post("/api/box/data/save_comments", headers=ha, json=encode(
        {"items": [_comment("lingshan", "w1", "c1"), _comment("lingshan", "w1", "c2")]})))) == [2, 0]
    _ok(c.post("/api/box/data/save_works", headers=hb, json=encode({"items": [_work("yuantouzhu", "w3")]})))
    _ok(c.post("/api/box/data/save_comments", headers=hb,
               json=encode({"items": [_comment("yuantouzhu", "w3", "c3")]})))
    since = datetime(2026, 1, 1)
    assert sorted(decode(_ok(c.post("/api/box/data/work_ids_collected_since", headers=ha, json=encode(
        {"scenic_id": "lingshan", "channel": "douyin", "since": since}))))) == ["w1"]
    assert c.post("/api/box/data/deleted_work_keys", headers=ha,
                  json={"scenic_id": "yuantouzhu", "channel": "douyin"}).status_code == 403

    # 记下了是哪个盒子报的（只在库里）
    boxes = {x["box_name"]: x for x in _ok(c.get("/api/boxes"))}
    assert boxes["灵山 1号盒子"]["uploaded_works"] == 2 and boxes["灵山 1号盒子"]["uploaded_comments"] == 2
    assert boxes["灵山 1号盒子"]["online"] is True

    # ---- 盒子查数据：只看得到自己景区的；主看全部
    works = _ok(c.get("/api/data/works", headers=ha))
    assert {w["scenic_id"] for w in works["items"]} == {"lingshan"} and works["total"] == 2
    assert _ok(c.get("/api/data/works", params={"scenic_id": "yuantouzhu"}, headers=ha))["total"] == 0
    assert _ok(c.get("/api/data/works"))["total"] == 3
    assert [r["scenic_id"] for r in _ok(c.get("/api/data/overview", headers=ha))] == ["lingshan"]
    trend = _ok(c.get("/api/data/trend", params={"days": 3}, headers=ha))
    assert sum(p["works"] for p in trend) == 2 and sum(p["comments"] for p in trend) == 2

    # 标注：列表只有自己的；别的景区那条看不到、改不了
    mine = _ok(c.get("/api/labeling/comments", headers=ha))
    assert {x["scenic_id"] for x in mine["items"]} == {"lingshan"} and mine["total"] == 2
    other = _ok(c.get("/api/labeling/comments", headers=hb))["items"][0]
    assert c.get(f"/api/labeling/comments/{other['id']}", headers=ha).status_code == 404
    assert c.put(f"/api/labeling/comments/{other['id']}", headers=ha,
                 json={"sentiment_label": "正向"}).status_code == 404
    assert c.post(f"/api/labeling/comments/{other['id']}/reset", headers=ha).status_code == 404
    saved = _ok(c.put(f"/api/labeling/comments/{mine['items'][0]['id']}",
                      headers={**ha, "X-SMC-Box-User": "%E5%BC%A0%E4%B8%89"},
                      json={"sentiment_label": "负向"}))
    assert saved["sentiment_label"] == "负向" and saved["label_review_by"] == "张三"
    # 删别的景区的作品：当作不存在
    other_work = _ok(c.get("/api/data/works", headers=hb))["items"][0]
    assert c.delete(f"/api/data/works/{other_work['id']}", headers=ha).status_code == 404

    # 停用盒子：令牌立刻失效
    _ok(c.put(f"/api/boxes/{b['box']['box_id']}", json={"enabled": False}))
    assert c.get("/api/box/assignment", headers=hb).status_code == 401
    # 重置令牌：旧的失效
    new_token = _ok(c.post(f"/api/boxes/{a['box']['box_id']}/token"))["token"]
    assert c.get("/api/box/assignment", headers=ha).status_code == 401
    ha = {"X-SMC-Box-Token": new_token}

    # ---- 盒子同步分配（在应用自己的事件循环里跑；盒子的库这里就是同一个测试库）
    from app.api.deps import get_state
    from app.box.client import MasterClient
    from app.box.sync import BoxSync

    def via_testclient(request: httpx.Request) -> httpx.Response:
        r = c.request(request.method, request.url.path, headers=dict(request.headers),
                      content=request.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    class _SyncTransport(httpx.AsyncBaseTransport):
        """应用的事件循环里不能再同步调 TestClient：转到线程里调。"""
        async def handle_async_request(self, request):
            await request.aread()
            return await asyncio.to_thread(via_testclient, request)

    state = get_state()
    sync = BoxSync(MasterClient("http://master", new_token, retry_delays=(), transport=_SyncTransport()),
                   state.db, state.scenics, state.settings)
    status = c.portal.call(sync.sync_once)
    assert status["ok"] is True, status
    record = c.portal.call(E.assignment, state.settings)
    assert record["scenic_ids"] == ["lingshan"] and record["box_name"] == "灵山 1号盒子"
    assert E.info(state.config)["box_name"] == "灵山 1号盒子"

    # 主改了分配：灵山 → 鼋头渚；盒子再同步一次，灵山停用、鼋头渚启用
    _ok(c.put(f"/api/boxes/{a['box']['box_id']}", json={"scenic_ids": ["yuantouzhu"]}))
    assert c.portal.call(sync.sync_once)["ok"] is True
    enabled = {s["scenic_id"] for s in c.portal.call(state.scenics.list_scenic_options)}
    assert enabled == {"yuantouzhu"}
    # 再拉一次、内容没变：不重新导入
    before = c.portal.call(E.assignment, state.settings)["imported_at"]
    assert c.portal.call(sync.sync_once)["ok"] is True
    assert c.portal.call(E.assignment, state.settings)["imported_at"] == before
    E._current = "full"
    E.set_synced_box_name("")
