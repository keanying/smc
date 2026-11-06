# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""归因明细（attribution_details）跟着 keyword_tags 走：入库、人工复核、重新识别标注字、导出。连真实 MySQL。"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime

import pytest

from app.core import attribution

TEST_DB = "scenic_media_attribution_test"
TABLE = "src_opinion_social_work_comment_di"


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


def _comment(cid, content):
    from app.collectors.base import CommentItem
    return CommentItem(channel="douyin", comment_id=cid, work_id="w1", scenic_id="hlx",
                       scenic_name="海蓝星梦幻城", content=content,
                       publish_time=datetime(2026, 10, 30), crawl_time=datetime.now()).finalize()


def _row(c, cid):
    from app.api.deps import get_state
    rows = c.portal.call(get_state().db.fetch_all,
                         f"SELECT id, keyword_tags, attribution_details FROM `{TABLE}` WHERE comment_id = %s", [cid])
    r = rows[0]
    return r["id"], json.loads(r["keyword_tags"] or "[]"), r["attribution_details"]


def _apply(c):
    job = _ok(c.post("/api/scenics/hlx/mark-words/apply"))
    for _ in range(200):
        job = _ok(c.get("/api/scenics/hlx/mark-words/apply"))
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    return job


def test_attribution_follows_keywords(app_client):
    from app.api.deps import get_state
    c, state = app_client, get_state()
    _ok(c.post("/api/scenics", json={"scenic_id": "hlx", "scenic_name": "海蓝星梦幻城"}))
    _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海蓝星"]}))

    # 1. 入库：还没 AI 标注，关键词只有标注字，归因写「命中本景区标注字」
    c.portal.call(state.data.save_comments, [_comment("c1", "海蓝星的海豹表演好看，整体一般般吧"),
                                             _comment("c2", "梦梦好萌")])
    row_id, kw, attr = _row(c, "c1")
    assert kw == ["海蓝星"]
    assert json.loads(attr) == [{"word": "海蓝星", "reason": attribution.MARK_WORD_REASON}]
    assert _row(c, "c2")[2] is None                         # 没关键词就没明细

    # 2. 模拟 AI 标注写回（引擎按 storage.columns 写这两列）；c2 模拟没有明细的历史数据
    c.portal.call(state.db.execute,
                  f"UPDATE `{TABLE}` SET sentiment_label='中性', sentiment_score=0, keyword_tags=%s, "
                  f"attribution_details=%s WHERE id=%s",
                  [json.dumps(["一般般", "海蓝星"], ensure_ascii=False),
                   json.dumps([{"word": "一般般", "reason": "景区整体感受一般"},
                               {"word": "海蓝星", "reason": attribution.MARK_WORD_REASON}], ensure_ascii=False),
                   row_id])
    c.portal.call(state.db.execute,
                  f"UPDATE `{TABLE}` SET sentiment_label='正向', sentiment_score=1, keyword_tags=%s WHERE comment_id='c2'",
                  ['["梦梦好萌"]'])
    item = _ok(c.get(f"/api/labeling/comments/{row_id}"))
    assert item["attribution_details"][0] == {"word": "一般般", "reason": "景区整体感受一般"}

    # 3. 人工复核改关键词：留下的原因不变、新加的记「人工复核添加」、删掉的跟着删
    _ok(c.put(f"/api/labeling/comments/{row_id}", json={"keyword_tags": ["一般般", "海豹表演好看"]}))
    _, kw, attr = _row(c, "c1")
    assert kw == ["一般般", "海豹表演好看"]
    assert json.loads(attr) == [{"word": "一般般", "reason": "景区整体感受一般"},
                                {"word": "海豹表演好看", "reason": attribution.HUMAN_REASON}]

    # 4. 加标注字后重新识别：AI / 人工的原因原样，补进来的标注字写标注字原因
    _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海豹"]}))
    result = _apply(c)
    _, kw, attr = _row(c, "c1")
    assert kw == ["一般般", "海豹表演好看", "海蓝星", "海豹"]
    assert json.loads(attr) == [
        {"word": "一般般", "reason": "景区整体感受一般"},
        {"word": "海豹表演好看", "reason": attribution.HUMAN_REASON},
        {"word": "海蓝星", "reason": attribution.MARK_WORD_REASON},
        {"word": "海豹", "reason": attribution.MARK_WORD_REASON},
    ]
    # 没命中标注字、也没有明细的历史行不去动它
    assert _row(c, "c2")[2] is None and result["changed"] == 1
    assert _apply(c)["changed"] == 0                         # 再点一次什么都不变

    # 5. 导出：归因明细和导出的关键词数组一一对应
    text = c.get("/api/data/export/comments", params={"scenic_id": "hlx"}).content.decode("utf-8-sig")
    exported = {r["评论ID"]: r for r in csv.DictReader(io.StringIO(text))}
    words = json.loads(exported["c1"]["关键词数组"])
    details = json.loads(exported["c1"]["归因明细"])
    assert [d["word"] for d in details] == words
    assert json.loads(exported["c2"]["归因明细"]) == [{"word": "梦梦好萌", "reason": ""}]
