# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""AI 过滤：采集时让模型判作品是不是在讲本景区（core/ai_relevance.py）。

- 任务上开了才判；不相关的丢，判不了的（没配模型、模型挂了、输出看不懂）按相关留下；
- 同一条作品只问一次模型；
- 景区盒子转给主判，主只给它判分到的景区。
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from app.core import ai_relevance as A

SCENIC = {"scenic_id": "jys", "scenic_name": "九嶷山舜帝陵风景名胜区", "province": "湖南省",
          "city": "永州市", "words": ["九嶷山", "宁远舜帝陵", "舜源峰"]}


class FakeLLM:
    def __init__(self, *texts, error=None):
        self.texts = list(texts)
        self.error = error
        self.prompts = []

    def complete(self, messages, max_output_tokens=None):
        self.prompts.append(messages[0]["content"][0]["text"])
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.texts.pop(0))


def test_prompt_and_parse():
    prompt = A.build_prompt(SCENIC, channel="douyin", title="山西运城舜帝陵", description="正" * 2000, label="舜帝")
    assert "九嶷山舜帝陵风景名胜区（湖南省永州市）" in prompt and "宁远舜帝陵、舜源峰" in prompt
    assert "别的省市的同名景区" in prompt and "拿不准的，算相关" in prompt
    assert "正" * A.MAX_TEXT + "…" in prompt and "正" * (A.MAX_TEXT + 1) not in prompt
    assert A.parse_verdict('{"relevant": false, "reason": "山西运城的舜帝陵"}').keep is False
    assert A.parse_verdict('好的：{"relevant": true, "reason": "游记"}').relevant is True
    assert A.parse_verdict('{"relevant": "false"}').relevant is False
    assert A.parse_verdict("不知道").relevant is None and A.parse_verdict("不知道").keep is True
    assert A.parse_verdict('{"reason": "x"}').relevant is None


def test_judge_cache_and_fail_open():
    llm = FakeLLM('{"relevant": false, "reason": "运城"}', '{"relevant": true, "reason": "游记"}')
    judge = A.RelevanceJudge(config=None, client=llm)
    v = asyncio.run(judge.judge(SCENIC, channel="douyin", work_id="w1", title="运城舜帝陵"))
    assert v.relevant is False and v.reason == "运城"
    # 同一条再问：不打模型
    assert asyncio.run(judge.judge(SCENIC, channel="douyin", work_id="w1")).relevant is False
    assert len(llm.prompts) == 1
    assert asyncio.run(judge.judge(SCENIC, channel="douyin", work_id="w2", title="九嶷山游记")).relevant is True
    # 模型挂了：判不了、按相关留下，而且不记住（下次再问）
    broken = A.RelevanceJudge(config=None, client=FakeLLM(error=RuntimeError("503")))
    v = asyncio.run(broken.judge(SCENIC, channel="douyin", work_id="w3"))
    assert v.relevant is None and v.keep and "模型调用失败" in v.reason
    asyncio.run(broken.judge(SCENIC, channel="douyin", work_id="w3"))
    assert len(broken._client.prompts) == 2
    # 输出看不懂：同样留下
    assert asyncio.run(A.RelevanceJudge(None, client=FakeLLM("嗯")).judge(
        SCENIC, channel="weibo", work_id="w4")).keep is True


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)

    def warn(self, m):
        self.lines.append(m)


class _Scenics:
    async def get_scenic(self, sid):
        return {"scenic_id": sid, "scenic_name": "九嶷山舜帝陵风景名胜区", "province": "湖南省", "city": "永州市"}

    async def list_keywords(self, sid, enabled_only=True):
        return [{"keyword": "九嶷山舜帝陵风景名胜区"}]

    async def filter_words_for(self, sid):
        return {"aux": ["九嶷山"], "exclude": ["运城"]}


def test_runner_hook():
    from app.scheduler.runner import TaskRunner
    runner = TaskRunner.__new__(TaskRunner)
    runner.scenics = _Scenics()
    seen = []

    class Judge:
        async def judge(self, scenic, **kw):
            seen.append((scenic, kw))
            return {"w-bad": A.Verdict(False, "别处的舜帝陵"), "w-unsure": A.Verdict(None, "模型挂了")}.get(
                kw["work_id"], A.Verdict(True, "游记"))

    runner.relevance = Judge()
    collector = SimpleNamespace(channel="douyin")
    work = lambda wid: SimpleNamespace(work_id=wid, title=f"标题{wid}", description="", label="")  # noqa: E731
    log, stats = _Log(), {"dropped": 0, "unsure": 0}

    ctx_off = SimpleNamespace(scenic_id="jys", params={})
    assert asyncio.run(runner._ai_keeps(ctx_off, collector, work("w-bad"), log, stats)) is True
    assert seen == []                                   # 没开：不问模型

    ctx = SimpleNamespace(scenic_id="jys", params={"ai_filter": True})
    assert asyncio.run(runner._ai_keeps(ctx, collector, work("w-good"), log, stats)) is True
    assert asyncio.run(runner._ai_keeps(ctx, collector, work("w-bad"), log, stats)) is False
    assert asyncio.run(runner._ai_keeps(ctx, collector, work("w-unsure"), log, stats)) is True
    assert stats == {"dropped": 1, "unsure": 1}
    assert any("[AI 过滤] 丢弃《标题w-bad》——别处的舜帝陵" in line for line in log.lines)
    # 景区信息只拼一次，给模型看的有省市和主 / 附关键字
    scenic = seen[0][0]
    assert scenic["province"] == "湖南省" and scenic["words"] == ["九嶷山舜帝陵风景名胜区", "九嶷山"]
    assert all(s is scenic for s, _ in seen)


def test_param_normalize():
    from app.core.collect_params import normalize
    assert normalize({"ai_filter": "true"}, ["douyin"])["ai_filter"] is True
    assert normalize({"ai_filter": 0}, ["douyin"])["ai_filter"] is False
    assert "ai_filter" not in normalize({}, ["douyin"])


# ---------------------------------------------------------------- 盒子转给主判
TEST_DB = "scenic_media_ai_filter_test"


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


def test_box_asks_master(app_client):
    from app.api.deps import get_state
    c = app_client
    state = get_state()
    assert isinstance(state.runner.relevance, A.RelevanceJudge)   # 主：本机问模型
    llm = FakeLLM('{"relevant": false, "reason": "山西运城的"}')
    state.runner.relevance = A.RelevanceJudge(config=None, client=llm)
    _ok(c.post("/api/scenics", json={"scenic_id": "jys", "scenic_name": "九嶷山舜帝陵风景名胜区",
                                     "province": "湖南省", "city": "永州市"}))
    _ok(c.post("/api/scenics", json={"scenic_id": "other", "scenic_name": "别的景区"}))
    token = _ok(c.post("/api/boxes", json={"box_name": "九嶷山 1号盒子", "scenic_ids": ["jys"]}))["token"]
    h = {"X-SMC-Box-Token": token}
    got = _ok(c.post("/api/box/ai-relevance", headers=h, json={
        "scenic_id": "jys", "channel": "douyin", "work_id": "w1", "title": "运城舜帝陵祭祖"}))
    assert got == {"relevant": False, "reason": "山西运城的"}
    assert "湖南省永州市" in llm.prompts[0] and "运城舜帝陵祭祖" in llm.prompts[0]
    # 没分给它的景区：不判
    assert c.post("/api/box/ai-relevance", headers=h, json={"scenic_id": "other"}).status_code == 403

    # 盒子那边的客户端：主说不相关就丢；主连不上按相关留下
    import httpx
    from app.box.client import MasterClient, RemoteRelevanceJudge
    ok_transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "code": 0, "data": json.loads(r.content) | {"relevant": False, "reason": "别处的"}}))
    remote = RemoteRelevanceJudge(MasterClient("http://master", "t", retry_delays=(), transport=ok_transport))
    v = asyncio.run(remote.judge(SCENIC, channel="douyin", work_id="w9", title="x"))
    assert v.relevant is False and v.reason == "别处的"
    down = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    remote = RemoteRelevanceJudge(MasterClient("http://master", "t", retry_delays=(), transport=down))
    assert asyncio.run(remote.judge(SCENIC, channel="douyin", work_id="w9")).keep is True


def test_toggle_from_task_list(app_client):
    """任务列表上一键开 / 关：只动 ai_filter，别的参数不变；纯点评任务不给开。"""
    c = app_client
    task = {"task_name": "九嶷山-抖音", "scenic_id": "jys", "collect_type": "keyword", "channels": ["douyin"],
            "keywords": ["九嶷山"], "schedule_type": "once", "params": {"max_works": 30, "ai_filter": False}}
    tid = _ok(c.post("/api/tasks", json={**task, "schedule_type": "cron", "cron_expression": "0 14 * * *"}))["task_id"]
    r = _ok(c.put(f"/api/tasks/{tid}/ai-filter", params={"enabled": True}))
    assert r["ai_filter"] is True
    params = _ok(c.get(f"/api/tasks/{tid}"))["params"]
    assert params["ai_filter"] is True and params["max_works"] == 30
    _ok(c.put(f"/api/tasks/{tid}/ai-filter", params={"enabled": False}))
    assert _ok(c.get(f"/api/tasks/{tid}"))["params"]["ai_filter"] is False
    _ok(c.post(f"/api/scenics/jys/targets", json={"channel": "ctrip", "target_type": "poi", "target_id": "1"}))
    poi = _ok(c.post("/api/tasks", json={**task, "task_name": "携程", "channels": ["ctrip"], "collect_type": "poi",
                                         "keywords": []}))["task_id"]
    assert c.put(f"/api/tasks/{poi}/ai-filter", params={"enabled": True}).status_code == 400
    assert c.put("/api/tasks/nope/ai-filter", params={"enabled": True}).status_code == 404
