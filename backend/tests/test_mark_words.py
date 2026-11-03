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
    assert mark_words.match("斑海豹“梦梦”在剧中", ["海豹梦梦"]) == ["海豹梦梦"]
    assert mark_words.match("《小城 良方》", ["小城良方"]) == ["小城良方"]
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


def _apply(c):
    """点「重新识别」，等后台跑完，返回最后的进度。"""
    import time
    job = _ok(c.post("/api/scenics/hlx/mark-words/apply"))
    assert job["status"] in ("running", "done") and job["total"] >= 1
    for _ in range(200):
        job = _ok(c.get("/api/scenics/hlx/mark-words/apply"))
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["percent"] == 100, job
    return job


def _marks(c):
    items = _ok(c.get("/api/labeling/comments", params={"scenic_id": "hlx", "page_size": 50}))["items"]
    return {x["comment_id"]: x["mark_word_tags"] for x in items}


def _kw(c):
    """库里 keyword_tags 列（不是页面合并出来的）"""
    from app.api.deps import get_state
    rows = c.portal.call(get_state().db.fetch_all,
                         "SELECT comment_id, keyword_tags FROM `src_opinion_social_work_comment_di` WHERE scenic_id = 'hlx'")
    return {r["comment_id"]: mark_words.loads(r["keyword_tags"]) for r in rows}


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
    # 补进了库里的 keyword_tags（还没 AI 标注，先只有标注字）
    assert _kw(c) == {"c0": [], "c1": ["小城良方"], "c2": [], "c3": ["肖战", "小城良方", "海蓝星"]}

    # 重新识别历史评论
    result = _apply(c)
    assert result["scanned"] == result["total"] == 4 and result["hit"] == 3 and result["changed"] == 1
    assert _marks(c)["c0"] == ["肖战", "小城良方"] and _kw(c)["c0"] == ["肖战", "小城良方"]

    # AI 提过关键词（这里用人工改来模拟），再补一个标注字重新识别：AI 的留着，标注字补在后面
    rows = c.portal.call(state.db.fetch_all,
                         "SELECT id FROM `src_opinion_social_work_comment_di` WHERE comment_id = 'c1'")
    row_id = rows[0]["id"]
    _ok(c.put(f"/api/labeling/comments/{row_id}", json={"sentiment_label": "正向", "keyword_tags": ["厉害了"]}))
    _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海豹"]}))
    result = _apply(c)
    assert result["changed"] == 2
    assert _marks(c)["c1"] == ["海豹", "小城良方"] and _kw(c)["c1"] == ["厉害了", "海豹", "小城良方"]
    item = _ok(c.get(f"/api/labeling/comments/{row_id}"))
    assert item["keyword_tags"] == ["厉害了", "海豹", "小城良方"]

    # 导出：关键词数组就是库里那列（已含标注字，不重复），另有「标注字」一列
    text = c.get("/api/data/export/comments", params={"scenic_id": "hlx"}).content.decode("utf-8-sig")
    exported = {r["评论ID"]: r for r in csv.DictReader(io.StringIO(text))}
    assert exported["c1"]["关键词数组"] == '["厉害了", "海豹", "小城良方"]'
    assert exported["c1"]["标注字"] == '["海豹", "小城良方"]'
    assert exported["c2"]["标注字"] == "[]"

    # 停用一个标注字再识别：标注字和 keyword_tags 里补的都拿掉，AI 的不动
    words = _ok(c.get("/api/scenics/hlx/filter-words", params={"kind": "mark"}))
    hb = next(w for w in words if w["word"] == "海豹")
    _ok(c.put(f"/api/scenics/filter-words/{hb['id']}/enabled", params={"enabled": False}))
    _apply(c)
    assert _marks(c)["c1"] == ["小城良方"] and _kw(c)["c1"] == ["厉害了", "小城良方"]
    # 再点一次：什么都不变
    assert _apply(c)["changed"] == 0

    # 引号、书名号不影响匹配（用户实测：「斑海豹“梦梦”」要命中「海豹梦梦」）
    _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": ["海豹梦梦"]}))
    c.portal.call(state.data.save_comments, [_comment("c4", "斑海豹“梦梦”在剧中上演荒诞一幕")])
    assert _marks(c)["c4"] == ["海豹梦梦"] and _kw(c)["c4"] == ["海豹梦梦"]

    # 上限 300
    r = _ok(c.post("/api/scenics/hlx/filter-words", json={"kind": "mark", "words": [f"词{i}" for i in range(400)]}))
    assert r["dropped"] > 0


def test_engine_matches_the_same_way():
    """标注引擎出口补标注字用的匹配，和采集系统的必须一模一样，不然两边补出来的对不上。"""
    from app.labeling.config_bridge import engine_importable, ensure_on_path
    if not engine_importable():
        pytest.skip("标注引擎目录不完整")
    ensure_on_path()
    from opinion_labeling_engine.labeling.scenic_rules import match_marks, merge_marks
    words = ["海蓝星", "肖战", "小城良方", "海豹梦梦", "Ocean", "海蓝星梦幻城"]
    for text in ("斑海豹“梦梦”在剧中", "肖战刘铮亮小城良方海豹取景地，海蓝星见", "去 OCEAN 馆",
                 "《海蓝星·梦幻城》", "梦梦好萌", ""):
        assert match_marks(text, words) == mark_words.match(text, words), text
    assert merge_marks(["厉害了", "小城良方"], ["小城良方", "海蓝星"]) == ["厉害了", "小城良方", "海蓝星"]
