# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""按景区现在的关键字重新过滤已采作品（scripts/refilter_works.py / app/utils/refilter.py）。

- 预览不删；--apply 才删，作品连评论一起删；
- 判定和采集时一样：标题 + 描述 + 标签含任一启用的主 / 附关键字才留，留下的再看过滤词；
- 携程等点评挂载点、按主页采的作品不动；停用的词不算数；--remember 记进已删除表。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

TEST_DB = "scenic_media_refilter_test"
ROOT = Path(__file__).resolve().parents[2]


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


SID = "PFTSCA01016184"


def _seed(c):
    from app.api.deps import get_state
    from app.collectors.base import CommentItem, WorkItem
    state = get_state()
    _ok(c.post("/api/scenics", json={"scenic_id": SID, "scenic_name": "九嶷山舜帝陵风景名胜区"}))
    _ok(c.post(f"/api/scenics/{SID}/keywords", json={"keywords": ["九嶷山舜帝陵风景名胜区", "宁远舜帝陵"]}))
    _ok(c.post(f"/api/scenics/{SID}/filter-words", json={"kind": "aux", "words": ["九嶷山", "九疑山", "舜源峰"]}))
    _ok(c.post(f"/api/scenics/{SID}/filter-words", json={"kind": "exclude", "words": ["运城", "加微信"]}))

    def work(wid, title, channel="douyin", keyword="九嶷山舜帝陵风景名胜区", label=""):
        return WorkItem(channel=channel, work_id=wid, scenic_id=SID, scenic_name="九嶷山", title=title,
                        description=title, label=label, source_keyword=keyword, hit_keywords=keyword,
                        crawl_time=datetime.now()).finalize()

    works = [
        work("keep1", "五一去了九嶷山，风景太美了"),
        work("keep2", "九疑山徒步一日游", channel="xiaohongshu"),
        work("keep3", "清晨的舜源峰", channel="weibo"),
        work("keep4", "旅行vlog", label="宁远舜帝陵,打卡"),                # 只在标签里命中
        work("drop1", "点斑祛斑什么时候比较好"),                            # 不含关键字
        work("drop2", "九嶷山和山西运城舜帝陵有什么区别", keyword="舜帝陵"),  # 含九嶷山，但命中过滤词「运城」
        work("drop3", "九嶷山特产 加微信下单", channel="kuaishou"),           # 相关但命中过滤词
        work("poi", "携程点评", channel="ctrip", keyword=""),                # 点评挂载点：不动
        work("home", "作者主页的随手拍", keyword=""),                        # 按主页采的：默认不动
    ]
    c.portal.call(state.data.save_works, works)
    comments = []
    for w in works:
        for i in range(3):
            comments.append(CommentItem(channel=w.channel, comment_id=f"{w.work_id}-c{i}", work_id=w.work_id,
                                        scenic_id=SID, content="评论", crawl_time=datetime.now()).finalize())
    c.portal.call(state.data.save_comments, comments)
    return state


def _work_ids(state):
    rows = _run(state, "SELECT work_id FROM `src_opinion_social_work_di` WHERE scenic_id = %s ORDER BY work_id")
    return [r["work_id"] for r in rows]


def _run(state, sql, args=None):
    from app.api.deps import get_state
    return _CLIENT.portal.call(get_state().db.fetch_all, sql, args or [SID])


_CLIENT = None


def test_refilter(app_client):
    global _CLIENT
    _CLIENT = c = app_client
    state = _seed(c)
    from app.utils.refilter import refilter_scenic

    # 预览：只算不删
    report = c.portal.call(lambda: refilter_scenic(state.db, SID))
    reasons = {d["work_id"]: d["reason"] for d in report["delete"]}
    assert reasons == {"drop1": "不含关键字", "drop2": "命中过滤词「运城」", "drop3": "命中过滤词「加微信」"}
    assert report["kept"] == 4 and report["skipped_homepage"] == 1
    assert all(d["comments"] == 3 for d in report["delete"])
    assert len(_work_ids(state)) == 9

    # 停用一个过滤词：它不再算数
    words = _ok(c.get(f"/api/scenics/{SID}/filter-words", params={"kind": "exclude"}))
    wx = next(w for w in words if w["word"] == "加微信")
    _ok(c.put(f"/api/scenics/filter-words/{wx['id']}/enabled", params={"enabled": False}))
    report = c.portal.call(lambda: refilter_scenic(state.db, SID))
    assert {d["work_id"] for d in report["delete"]} == {"drop1", "drop2"}

    # 真删：作品连评论一起；点评挂载点、主页采的、相关的都在
    report = c.portal.call(lambda: refilter_scenic(state.db, SID, apply=True, remember=True))
    assert report["deleted_works"] == 2 and report["deleted_comments"] == 6
    assert _work_ids(state) == ["drop3", "home", "keep1", "keep2", "keep3", "keep4", "poi"]
    left = _run(state, "SELECT COUNT(*) AS n FROM `src_opinion_social_work_comment_di` WHERE scenic_id = %s")
    assert left[0]["n"] == 7 * 3
    remembered = _run(state, "SELECT work_id FROM `src_opinion_social_work_deleted` WHERE scenic_id = %s ORDER BY work_id")
    assert [r["work_id"] for r in remembered] == ["drop1", "drop2"]
    # 再跑一遍：没有要删的
    assert c.portal.call(lambda: refilter_scenic(state.db, SID))["delete"] == []


def test_cli(app_client):
    """命令行：预览不删、出 CSV；--apply --yes 才删。"""
    from app.api.deps import get_state
    state = get_state()
    _ok(app_client.post(f"/api/scenics/{SID}/filter-words", json={"kind": "exclude", "words": ["特产"]}))
    env = {**os.environ, "SMC_MYSQL_DATABASE": TEST_DB}
    cmd = [sys.executable, str(ROOT / "scripts" / "refilter_works.py"), "--scenic", SID]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "要删 1 条作品" in out.stdout and "这是预览" in out.stdout and ".csv" in out.stdout
    assert "drop3" in _work_ids(state)
    out = subprocess.run(cmd + ["--apply", "--yes"], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "删了 1 条作品、3 条评论" in out.stdout
    assert "drop3" not in _work_ids(state)
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "refilter_works.py"), "--scenic", "NOPE"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "没有这些景区" in bad.stdout
