# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""标注字：景区配的词在评论原文里出现就提取，给 AI 关键词做补充。

- 只看评论原文，没提到的不打；按原文里出现的先后排，英文不分大小写；
- 新评论入库时自动识别；补了标注字后「重新识别」把历史评论按新词表重算；
- AI 的关键词列不动（AI 重标会整列覆盖它）；页面 / 导出里合并显示，导出另有「标注字」一列。
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime

import pytest

from app.core import mark_words

TEST_DB = "scenic_media_mark_words_test"


def test_match_and_merge():
    words = ["海蓝星", "肖战", "小城良方", "Ocean"]
    assert mark_words.match("一部剧海豹都火了，厉害了这个《小城良方》", words) == ["小城良方"]
    assert mark_words.match("肖战刘铮亮小城良方海豹取景地，就在海蓝星", words) == ["肖战", "小城良方", "海蓝星"]
    assert mark_words.match("梦梦好萌", words) == []
    assert mark_words.match("去 OCEAN 馆", words) == ["Ocean"]
    assert mark_words.match("", words) == [] and mark_words.match("海蓝星", []) == []
    # AI 已经提到的不重复补；AI 关键词在前
    assert mark_words.merge_keywords(["厉害了", "小城良方"], '["小城良方", "海蓝星"]') == ["厉害了", "小城良方", "海蓝星"]
    assert mark_words.merge_keywords([], None) == []


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


def _marks(c):
    items = _ok(c.get("/api/labeling/comments", params={"scenic_id": "hlx", "page_size": 50}))["items"]
    return {x["comment_id"]: x["mark_word_tags"] for x in items}


def test_mark_words_flow(app_client):
    from app.api.deps import get_state
    c = app_client
    state = get_state()
    _ok(c.post("/api/scenics", json={"scenic_id": "hlx", "scenic_name": "海蓝星梦幻城"}))
    # 还没配标注字时采到的评论：什么都不打
    c.portal.call(state.data.save_comments, [_comment("c0", "肖战的小城良方太好看了")])
    assert _marks(c) == {"c0": []}

    r = _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海蓝星", "肖战", "小城良方"]}))
    assert r["added"] == 3
    # 新评论入库：自动识别
    c.portal.call(state.data.save_comments, [
        _comment("c1", "一部剧海豹都火了，厉害了这个《小城良方》"),
        _comment("c2", "梦梦好萌"),
        _comment("c3", "果然是肖战刘铮亮小城良方海豹取景地，海蓝星见"),
    ])
    marks = _marks(c)
    assert marks["c1"] == ["小城良方"] and marks["c2"] == [] and marks["c3"] == ["肖战", "小城良方", "海蓝星"]
    assert marks["c0"] == []          # 历史的要点「重新识别」才会补

    # 重新识别历史评论
    result = _ok(c.post("/api/scenics/hlx/mark-words/apply"))
    assert result["scanned"] == 4 and result["hit"] == 3 and result["changed"] == 1
    assert _marks(c)["c0"] == ["肖战", "小城良方"]

    # 补一个词再识别：只改受影响的行
    _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海豹"]}))
    result = _ok(c.post("/api/scenics/hlx/mark-words/apply"))
    assert result["changed"] == 2 and _marks(c)["c1"] == ["海豹", "小城良方"]

    # 原来的 AI 关键词不动；导出里合并，另有「标注字」一列
    rows = c.portal.call(state.db.fetch_all,
                               "SELECT id, comment_id FROM `src_opinion_social_work_comment_di` WHERE comment_id = 'c1'")
    row_id = rows[0]["id"]
    _ok(c.put(f"/api/labeling/comments/{row_id}", json={"sentiment_label": "正向", "keyword_tags": ["厉害了", "海豹"]}))
    item = _ok(c.get(f"/api/labeling/comments/{row_id}"))
    assert item["keyword_tags"] == ["厉害了", "海豹"] and item["mark_word_tags"] == ["海豹", "小城良方"]
    text = c.get("/api/data/export/comments", params={"scenic_id": "hlx"}).content.decode("utf-8-sig")
    exported = {r["评论ID"]: r for r in csv.DictReader(io.StringIO(text))}
    assert exported["c1"]["关键词数组"] == '["厉害了", "海豹", "小城良方"]'
    assert exported["c1"]["标注字"] == '["海豹", "小城良方"]'
    assert exported["c2"]["标注字"] == "[]"

    # 停用一个标注字再识别：去掉
    words = _ok(c.get("/api/scenics/hlx/filter-words", params={"kind": "mark"}))
    hb = next(w for w in words if w["word"] == "海豹")
    _ok(c.put(f"/api/scenics/filter-words/{hb['id']}/enabled", params={"enabled": False}))
    _ok(c.post("/api/scenics/hlx/mark-words/apply"))
    assert _marks(c)["c1"] == ["小城良方"]

    # 上限 300
    r = _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": [f"词{i}" for i in range(400)]}))
    assert r["dropped"] > 0
