# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""景区版：哪些接口卡住、总部导出分配包、盒子导入后景区和总部一致、再下发时停用不再分配的景区。

一个真实应用 + 独立测试库。总部和盒子用同一个进程模拟：先按总部版建景区、导出，
再把进程切成景区版导入（edition.set_current），省得起两个 TestClient（共用不了连接池）。
"""
from __future__ import annotations

import os

import pytest

from app import edition as E

TEST_DB = "scenic_media_edition_test"


def test_denied_rules():
    deny = lambda m, p: E.denied_reason("scenic", m, p)  # noqa: E731
    # 总部版什么都不卡
    assert E.denied_reason("full", "DELETE", "/api/scenics/x") == ""
    # 景区版：看可以，改不行
    assert deny("GET", "/api/scenics") == ""
    assert deny("POST", "/api/scenics")
    assert deny("DELETE", "/api/scenics/s1")
    assert deny("POST", "/api/archive/collect")
    assert deny("PUT", "/api/cluster/nodes/n1/accept")
    assert deny("POST", "/api/metrics/runs")
    assert deny("POST", "/api/qunar/import")
    assert deny("POST", "/api/settings/notify/test")
    # 账号额度要能改：PUT /api/settings 放过去，由接口再按段判
    assert deny("PUT", "/api/settings") == ""
    assert E.settings_allowed("scenic", "crawl", {"account_quota": {}})
    assert not E.settings_allowed("scenic", "crawl", {"account_quota": {}, "max_pages": 3})
    assert not E.settings_allowed("scenic", "proxy", {"enabled": True})
    assert E.settings_allowed("full", "proxy", {"enabled": True})
    # 盒子自己的事照常：任务、账号、标注
    for method, path in (("POST", "/api/tasks"), ("POST", "/api/accounts"),
                         ("PUT", "/api/labeling/comments/3"), ("POST", "/api/edition/bundle/import")):
        assert deny(method, path) == "", path
    assert deny("POST", "/api/edition/bundle/export")


def test_parse_bundle_rejects_garbage():
    with pytest.raises(ValueError, match="JSON"):
        E.parse_bundle("{not json")
    with pytest.raises(ValueError, match="分配包"):
        E.parse_bundle({"kind": "other"})
    with pytest.raises(ValueError, match="新版本"):
        E.parse_bundle({"kind": E.BUNDLE_KIND, "version": 99, "scenics": [{}]})
    with pytest.raises(ValueError, match="缺少"):
        E.parse_bundle({"kind": E.BUNDLE_KIND, "version": 1, "scenics": [{"scenic_id": "a"}]})


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


def test_bundle_round_trip(app_client):
    c = app_client
    E._current = "full"
    assert _ok(c.get("/api/edition"))["edition"] == "full"

    # ---- 总部：建两个景区，配好关键字、词表、目标，导出其中两个
    for sid, name in (("lingshan", "灵山胜境"), ("yuantouzhu", "鼋头渚")):
        _ok(c.post("/api/scenics", json={"scenic_id": sid, "scenic_name": name, "city": "无锡"}))
        _ok(c.post(f"/api/scenics/{sid}/keywords", json={"keywords": [name, f"{name}门票"]}))
    _ok(c.post("/api/scenics/lingshan/filter-words", json={"kind": "exclude", "words": ["广告"]}))
    _ok(c.post("/api/scenics/lingshan/targets", json={
        "channel": "ctrip", "target_type": "poi", "target_id": "123", "target_name": "灵山"}))
    r = c.post("/api/edition/bundle/export",
               json={"scenic_ids": ["lingshan", "yuantouzhu"], "box_name": "无锡 1 号盒子"})
    assert r.status_code == 200
    bundle = r.json()
    assert bundle["kind"] == E.BUNDLE_KIND and len(bundle["scenics"]) == 2
    ling = next(s for s in bundle["scenics"] if s["scenic_id"] == "lingshan")
    assert ling["keywords"] == ["灵山胜境", "灵山胜境门票"]
    assert ling["filter_words"]["exclude"] == ["广告"]
    assert ling["targets"][0]["target_id"] == "123"
    # 总部版不收分配包
    assert c.post("/api/edition/bundle/import", json=bundle).status_code == 400

    # ---- 切成盒子：景区改不了，系统设置改不了，导入分配包
    E._current = "scenic"
    assert _ok(c.get("/api/edition"))["edition"] == "scenic"
    assert c.post("/api/scenics", json={"scenic_id": "x", "scenic_name": "x"}).status_code == 403
    assert c.delete("/api/scenics/lingshan").status_code == 403
    assert c.post("/api/edition/bundle/export", json={"scenic_ids": ["lingshan"]}).status_code == 403
    assert c.put("/api/settings", json={"section": "proxy", "values": {"enabled": True}}).status_code == 403
    _ok(c.put("/api/settings", json={"section": "crawl",
                                     "values": {"account_quota": {"enabled": True}}}))

    result = _ok(c.post("/api/edition/bundle/import", json=bundle))
    assert result["scenic_ids"] == ["lingshan", "yuantouzhu"] and result["dropped"] == []
    info = _ok(c.get("/api/edition/assignment"))
    assert info["box_name"] == "无锡 1 号盒子"
    assert {s["scenic_id"] for s in info["scenics"]} == {"lingshan", "yuantouzhu"}

    # 任务：只能建在分配到的景区上
    task = {"task_name": "t", "collect_type": "keyword", "channels": ["ctrip"],
            "keywords": ["灵山"], "schedule_type": "once"}
    r = c.post("/api/tasks", json=task)
    assert r.status_code == 400 and "选一个景区" in r.json()["message"]
    _ok(c.post("/api/tasks", json={**task, "scenic_id": "lingshan"}))

    # ---- 总部重新下发：只剩灵山，关键字也改了 → 鼋头渚停用，灵山关键字换成新的，目标被撤掉
    ling2 = {**ling, "keywords": ["灵山大佛"], "targets": []}
    result = _ok(c.post("/api/edition/bundle/import", json={**bundle, "scenics": [ling2]}))
    assert result["dropped"] == ["yuantouzhu"]
    info = _ok(c.get("/api/edition/assignment"))
    assert [s["scenic_id"] for s in info["scenics"]] == ["lingshan"]
    kws = [k["keyword"] for k in _ok(c.get("/api/scenics/lingshan/keywords"))]
    assert kws == ["灵山大佛"]
    assert _ok(c.get("/api/scenics/lingshan/targets")) == []
    r = c.post("/api/tasks", json={**task, "scenic_id": "yuantouzhu"})
    assert r.status_code == 400 and "没有分配" in r.json()["message"]
