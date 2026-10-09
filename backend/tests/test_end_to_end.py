"""端到端：从建景区到导出 CSV 跑通完整链路。

用真实 MySQL + 真实 FastAPI 应用 + 本地假携程站点，覆盖：
    建景区 -> 加关键字 -> 配 POI 目标 -> 建任务 -> 执行 -> 落库
    -> 数据中心查询（作品/评论树）-> 导出 CSV
唯一被替换的是目标站点（沙箱访问不到 m.ctrip.com），
其余每一环都是生产代码本身。
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "ctrip_page.json").read_text(encoding="utf-8")
)

TEST_DB = "scenic_media_e2e"


class _FakeCtrip(BaseHTTPRequestHandler):
    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._json({"ClientID": "e2e-client-id-0001"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        page = payload.get("arg", {}).get("pageIndex", 1)
        self._json(FIXTURE if page == 1 else
                   {"code": 200, "result": {"totalCount": 23, "items": []}})

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def fake_ctrip():
    server = HTTPServer(("127.0.0.1", 0), _FakeCtrip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def client(fake_ctrip):
    # 用独立测试库，跑完即丢，不碰开发库
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"
    os.environ["SMC_SCHEDULER_ENABLED"] = "false"   # 手动触发，测试不等轮询

    from app.core.config import reset_cache
    reset_cache()

    from app.collectors.ctrip import CtripCollector
    CtripCollector.client_id_url = f"{fake_ctrip}/restapi/soa2/10290/createclientid"
    CtripCollector.comment_url = f"{fake_ctrip}/restapi/soa2/13444/json/getCommentCollapseList"

    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

    # 跑完把测试库整个丢掉：不丢的话，下次跑（或同一进程里别的用例
    # 复用这个库名时）会撞上上次残留的数据，出现
    # "关键字 added=0"、"作品数对不上" 这种和代码无关的假失败。
    # ⚠️ 不能用 asyncio.run：pytest-asyncio 的会话级事件循环会被它关掉，
    #    后面所有 async 用例都会报 "no current event loop"。
    #    用 pymysql 同步连接做这件事，不碰事件循环。
    import pymysql
    from app.core.config import load_config

    cfg = load_config(use_cache=False)
    conn = pymysql.connect(
        host=cfg.get("mysql.host", "127.0.0.1"),
        port=int(cfg.get("mysql.port", 3306)),
        user=cfg.get("mysql.user", "root"),
        password=cfg.get("mysql.password", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        conn.commit()
    finally:
        conn.close()
    reset_cache()


def _data(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def test_01_health(client):
    payload = _data(client.get("/api/health"))
    assert payload["status"] == "ok"
    assert payload["mysql"] is True
    assert "ctrip" in payload["collectors"]
    assert "tongcheng" in payload["collectors"]


def test_02_create_scenic_and_keywords(client):
    _data(client.post("/api/scenics", json={
        "scenic_id": "SC001", "scenic_name": "西湖风景区",
        "province": "浙江", "city": "杭州",
    }))

    result = _data(client.post("/api/scenics/SC001/keywords", json={
        "keywords": ["西湖", "西湖 攻略", "断桥残雪", "西湖", "  "],
    }))
    # 重复的"西湖"和空白项被清掉
    assert result["added"] == 3

    keywords = _data(client.get("/api/scenics/SC001/keywords"))
    assert [k["keyword"] for k in keywords] == ["西湖", "西湖 攻略", "断桥残雪"]

    # 再加一次全是重复的
    again = _data(client.post("/api/scenics/SC001/keywords", json={"keywords": ["西湖"]}))
    assert again["added"] == 0 and again["skipped"] == 1


def test_03_import_scenics_from_csv(client):
    csv_text = "景区ID,景区名称,省份,城市\nSC002,灵隐寺,浙江,杭州\nSC003,千岛湖,浙江,杭州\n"
    response = client.post(
        "/api/scenics/import-csv",
        files={"file": ("scenics.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )
    result = _data(response)
    assert result["created"] == 2

    listing = _data(client.get("/api/scenics"))
    assert listing["total"] >= 3
    names = {row["scenic_name"] for row in listing["items"]}
    assert {"西湖风景区", "灵隐寺", "千岛湖"} <= names


def test_04_configure_ctrip_poi(client):
    _data(client.post("/api/scenics/SC001/targets", json={
        "channel": "ctrip", "target_type": "poi",
        "target_id": "32289", "target_name": "西湖",
    }))
    targets = _data(client.get("/api/scenics/SC001/targets"))
    assert len(targets) == 1
    assert targets[0]["channel"] == "ctrip"
    assert targets[0]["target_id"] == "32289"


def test_05_reject_task_without_poi(client):
    """选了携程但景区没配 POI_ID，应当在创建时就拦下并说清楚怎么办。"""
    response = client.post("/api/tasks", json={
        "task_name": "灵隐寺携程点评", "scenic_id": "SC002",
        "channels": ["ctrip"], "collect_type": "poi",
    })
    assert response.status_code == 400
    assert "POI_ID" in response.json()["message"]


def test_06_cron_validation_and_preview(client):
    bad = client.post("/api/tasks", json={
        "task_name": "错误cron", "scenic_id": "SC001", "channels": ["ctrip"],
        "collect_type": "poi", "schedule_type": "cron", "cron_expression": "0 2 * *",
    })
    assert bad.status_code == 400
    assert "5 段" in bad.json()["message"]

    preview = _data(client.post(
        "/api/tasks/preview-schedule",
        params={"schedule_type": "cron", "cron_expression": "0 3 * * *", "count": 3},
    ))
    assert len(preview["next_runs"]) == 3
    assert all(run.endswith("03:00:00") for run in preview["next_runs"]), preview


def test_07_create_cron_task(client):
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖携程点评-每天3点", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi",
        "schedule_type": "cron", "cron_expression": "0 3 * * *",
    }))
    assert task["schedule_type"] == "cron"
    assert task["schedule_enabled"] == 1
    assert task["next_run_time"] is not None
    assert task["next_run_time"].endswith("03:00:00")


def test_08_run_task_and_collect(client):
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖携程点评-立即执行", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi",
        "schedule_type": "once",
        "params": {"max_comments_per_work": 100},
    }))
    task_id = task["task_id"]

    _data(client.post(f"/api/tasks/{task_id}/run"))

    # 等任务跑完
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.5)

    assert current["status"] == "completed", current.get("error")
    # 1 条合成作品 + 2 条一级评论 + 1 条二级回复
    assert current["stat_new_works"] == 1
    assert current["stat_new_comments"] == 3
    assert current["runs_count"] == 1

    logs = _data(client.get(f"/api/tasks/{task_id}/logs"))
    messages = " ".join(log["message"] for log in logs)
    assert "西湖风景区" in messages
    assert "采集到 3 条点评" in messages


def test_09_rerun_is_idempotent(client):
    """同一任务再跑一次：数据应该是更新而不是翻倍。"""
    listing = _data(client.get("/api/tasks", params={"keyword": "立即执行"}))
    task_id = listing["items"][0]["task_id"]

    before = _data(client.get("/api/data/comments", params={
        "channel": "ctrip", "work_id": "32289",
    }))["total"]

    _data(client.post(f"/api/tasks/{task_id}/run"))
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)

    after = _data(client.get("/api/data/comments", params={
        "channel": "ctrip", "work_id": "32289",
    }))["total"]
    assert after == before, "重复采集产生了重复数据"
    assert current["runs_count"] == 2


def test_10_data_center_overview(client):
    overview = _data(client.get("/api/data/overview"))
    xihu = next(row for row in overview if row["scenic_id"] == "SC001")
    assert xihu["scenic_name"] == "西湖风景区"
    ctrip = next(c for c in xihu["channels"] if c["channel"] == "ctrip")
    assert ctrip["work_cnt"] == 1
    assert ctrip["comment_cnt"] == 3


def test_11_works_and_comment_tree(client):
    works = _data(client.get("/api/data/works", params={
        "scenic_id": "SC001", "channel": "ctrip",
    }))
    assert works["total"] == 1
    work = works["items"][0]
    assert work["work_id"] == "32289"
    assert work["scenic_name"] == "西湖风景区"
    # 列表接口不再返回 extra_content（那是几十 KB 的原始 JSON，见
    # data_repo.WORK_LIST_COLUMNS），"是不是合成作品"由 SQL 直接算成布尔
    assert work["is_synthetic"] is True
    assert "extra_content" not in work, "列表又开始拉 LONGTEXT 了，页面会变慢"

    # 过滤掉合成作品后应该是 0 条
    real_works = _data(client.get("/api/data/works", params={
        "scenic_id": "SC001", "channel": "ctrip", "include_synthetic": False,
    }))
    assert real_works["total"] == 0

    # 一级评论
    level1 = _data(client.get("/api/data/comments", params={
        "channel": "ctrip", "work_id": "32289",
    }))
    assert level1["total"] == 2
    assert all(c["comment_level"] == "level_1" for c in level1["items"])

    parent = next(c for c in level1["items"] if c["comment_id"] == "901001")
    assert parent["sub_comment_count"] == 1

    # 点击展开：拿父评论ID取子评论
    children = _data(client.get("/api/data/comments", params={
        "channel": "ctrip", "work_id": "32289", "parent_id": "901001",
    }))
    assert children["total"] == 1
    assert children["items"][0]["comment_level"] == "level_2"
    assert children["items"][0]["commenter_name"] == "景区官方"

    # 一次性取整条会话，返回嵌套树
    thread = _data(client.get("/api/data/comments/thread", params={
        "channel": "ctrip", "work_id": "32289", "root_comment_id": "901001",
    }))
    assert len(thread) == 1
    assert thread[0]["comment_id"] == "901001"
    assert len(thread[0]["children"]) == 1
    assert thread[0]["children"][0]["comment_id"] == "901001901"


def test_12_export_csv(client):
    response = client.get("/api/data/export/comments", params={
        "scenic_id": "SC001", "channel": "ctrip",
    })
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    text = response.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    header, *body = rows

    assert header[:7] == [
        "景区ID", "景区名字", "平台", "作品ID", "评论等级", "评论父ID", "评论ID",
    ]
    # AI 标注字段建了列但本期不写值
    assert "整体情感标签" in header
    assert len(body) == 3

    by_id = {row[header.index("评论ID")]: row for row in body}
    first = by_id["901001"]
    assert first[header.index("景区名字")] == "西湖风景区"
    assert first[header.index("平台")] == "ctrip"
    assert "荷花" in first[header.index("评论内容")]
    assert first[header.index("点赞数")] == "12"
    assert first[header.index("发布地址")] == "浙江"
    assert first[header.index("发布时间")] == "2026-08-17 16:27:25"
    assert first[header.index("整体情感标签")] == ""

    # 英文表头版本
    english = client.get("/api/data/export/comments", params={
        "scenic_id": "SC001", "english_headers": True,
    })
    en_header = list(csv.reader(io.StringIO(english.content.decode("utf-8-sig"))))[0]
    assert en_header[:4] == ["scenic_id", "scenic_name", "channel", "work_id"]


def test_13_export_works_csv(client):
    response = client.get("/api/data/export/works", params={"scenic_id": "SC001"})
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    header, *body = rows
    assert header[:5] == ["景区ID", "景区名字", "平台", "作品ID", "作品链接"]
    assert len(body) == 1


def test_14_missing_account_reports_clearly(client):
    """需要登录的平台没配账号时，要给出可操作的提示而不是静默返回 0 条，
    并且单个平台不可用不能把整个任务判失败。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "抖音测试", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword",
        "schedule_type": "once",
    }))
    task_id = task["task_id"]
    _data(client.post(f"/api/tasks/{task_id}/run"))
    for _ in range(40):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)

    logs = _data(client.get(f"/api/tasks/{task_id}/logs"))
    messages = " ".join(log["message"] for log in logs)
    # 提示要说清楚"去哪儿做什么"，而不是抛一个裸异常
    assert "没有可用账号" in messages, messages
    assert "账号管理" in messages, messages
    # 单个平台不可用不应让整个任务失败
    assert current["status"] == "completed"


def test_15_settings_and_proxy_status(client):
    settings = _data(client.get("/api/settings"))
    assert "proxy" in settings["config"]
    assert "proxy" in settings["editable_sections"]

    _data(client.put("/api/settings", json={
        "section": "crawl", "values": {"default_max_works": 33},
    }))
    reread = _data(client.get("/api/settings"))
    assert reread["config"]["crawl"]["default_max_works"] == 33

    # mysql 段不允许在页面上改
    denied = client.put("/api/settings", json={
        "section": "mysql", "values": {"host": "evil"},
    })
    assert denied.status_code == 400

    status = _data(client.get("/api/settings/proxy/status"))
    assert status["enabled"] is False
    assert set(status["per_channel"]) >= {"ctrip", "douyin"}


def test_16_settings_persist_and_mask_secrets(client):
    _data(client.put("/api/settings", json={
        "section": "proxy",
        "values": {"secret_id": "my_secret_id", "secret_key": "my_secret_key"},
    }))
    settings = _data(client.get("/api/settings"))
    # 密钥回显必须打码
    assert settings["config"]["proxy"]["secret_id"] == "***"
    assert settings["config"]["proxy"]["secret_key"] == "***"


# ---------------------------------------------------------------------------
# 需求 4 / 5：按平台设采集数量、任务建好之后还能改
# ---------------------------------------------------------------------------

def test_17_rejects_multi_channel_tasks(client):
    """一个任务只能选一个平台。

    收紧的理由见 schemas._validate_single_channel：采集模式、账号占用、
    浏览器排队都是按平台算的，混在一条任务里既没法分别设置，
    失败了也说不清是哪个平台失败、没法单独重跑。
    """
    response = client.post("/api/tasks", json={
        "task_name": "西湖-两个平台", "scenic_id": "SC001",
        "channels": ["weibo", "ctrip"], "collect_type": "keyword",
        "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 4 * * *",
    })
    assert response.status_code == 422, response.text
    assert "只能选一个" in response.text

    # 改成一个平台就能建
    ok_response = client.post("/api/tasks", json={
        "task_name": "西湖-两个平台", "scenic_id": "SC001",
        "channels": ["weibo"], "collect_type": "keyword", "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 4 * * *",
    })
    assert ok_response.status_code == 200, ok_response.text


def test_17b_per_channel_collect_limits(client):
    """按平台设数量的机制仍在：一条任务一个平台，各自设各自的。"""
    weibo_task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-分平台数量", "scenic_id": "SC001",
        "channels": ["weibo"], "collect_type": "keyword",
        "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 4 * * *",
        "params": {
            "max_works": 100, "max_comments_per_work": 500,
            "channel_params": {"weibo": {"max_works": 20, "max_comments_per_work": 50}},
        },
    }))
    ctrip_task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-分平台数量-携程", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi",
        "schedule_type": "cron", "cron_expression": "0 4 * * *",
        "params": {
            "max_works": 100, "max_comments_per_work": 500,
            "channel_params": {"ctrip": {"max_comments": 3000}},
        },
    }))

    limits = {row["channel"]: row for row in
              weibo_task["collect_limits"] + ctrip_task["collect_limits"]}
    assert limits["weibo"]["has_works"] is True
    assert limits["weibo"]["max_works"] == 20
    assert limits["weibo"]["max_comments"] == 50
    # 携程没有作品这一层，作品数固定为 0（不限），点评数用 max_comments
    assert limits["ctrip"]["has_works"] is False
    assert limits["ctrip"]["max_comments"] == 3000
    assert limits["ctrip"]["max_works"] == 0

    # 给携程配 max_works 是无意义的键，不该被存进去
    stored = _data(client.get(f"/api/tasks/{ctrip_task['task_id']}"))["params"]
    assert "max_works" not in stored["channel_params"]["ctrip"]


def test_18_patch_changes_schedule_without_recreating(client):
    listing = _data(client.get("/api/tasks", params={"keyword": "分平台数量"}))
    task = listing["items"][0]
    task_id = task["task_id"]

    updated = _data(client.patch(f"/api/tasks/{task_id}", json={
        "schedule_type": "interval", "schedule_interval_seconds": 7200,
    }))

    assert updated["task_id"] == task_id, "改运行模式不该产生新任务"
    assert updated["schedule_type"] == "interval"
    assert updated["schedule_interval_seconds"] == 7200
    assert updated["next_run_time"] is not None
    # 没传的字段原样保留
    assert updated["task_name"] == task["task_name"]
    assert updated["keywords"] == task["keywords"]
    assert updated["channels"] == task["channels"]


def test_19_patch_changes_counts_only(client):
    listing = _data(client.get("/api/tasks", params={"keyword": "分平台数量-携程"}))
    task_id = listing["items"][0]["task_id"]

    updated = _data(client.patch(f"/api/tasks/{task_id}", json={
        "params": {
            "max_works": 100, "max_comments_per_work": 500,
            "channel_params": {"ctrip": {"max_comments": 88}},
        },
    }))

    limits = {row["channel"]: row for row in updated["collect_limits"]}
    assert limits["ctrip"]["max_comments"] == 88
    # 调度没动（上一条用例刚把它改成 interval）
    assert updated["schedule_type"] == "interval"
    assert updated["schedule_interval_seconds"] == 7200


def test_20_patch_rejects_bad_cron_and_empty_body(client):
    listing = _data(client.get("/api/tasks", params={"keyword": "分平台数量"}))
    task_id = listing["items"][0]["task_id"]

    bad = client.patch(f"/api/tasks/{task_id}", json={
        "schedule_type": "cron", "cron_expression": "not a cron",
    })
    assert bad.status_code == 400

    empty = client.patch(f"/api/tasks/{task_id}", json={})
    assert empty.status_code == 400

    # 被拒绝后任务保持原样，不能出现"模式改了但表达式没改"的半截状态
    current = _data(client.get(f"/api/tasks/{task_id}"))
    assert current["schedule_type"] == "interval"


def test_21_patch_drops_params_of_removed_channels(client):
    """把任务的平台从携程改成微博，携程那份数量配置要一并清掉，
    否则编辑页会一直显示一堆对当前平台毫无意义的键。"""
    listing = _data(client.get("/api/tasks", params={"keyword": "分平台数量-携程"}))
    task_id = listing["items"][0]["task_id"]

    updated = _data(client.patch(f"/api/tasks/{task_id}", json={"channels": ["weibo"]}))
    assert updated["channels"] == ["weibo"]
    assert "ctrip" not in (updated["params"].get("channel_params") or {})


def test_21b_patch_rejects_multi_channel(client):
    listing = _data(client.get("/api/tasks", params={"keyword": "分平台数量"}))
    task_id = listing["items"][0]["task_id"]
    response = client.patch(f"/api/tasks/{task_id}", json={"channels": ["weibo", "douyin"]})
    assert response.status_code == 422, response.text


def test_22_patch_takes_effect_on_next_run(client):
    """改完数量后再跑一次，任务日志里必须体现新的上限。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖携程-改数量验证", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "cron",
        "cron_expression": "0 5 * * *",
        "params": {"channel_params": {"ctrip": {"max_comments": 500}}},
    }))
    task_id = task["task_id"]

    _data(client.patch(f"/api/tasks/{task_id}", json={
        "params": {"channel_params": {"ctrip": {"max_comments": 2}}},
    }))

    _data(client.post(f"/api/tasks/{task_id}/run"))
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.5)
    assert current["status"] == "completed", current.get("error")

    messages = " ".join(
        log["message"] for log in _data(client.get(f"/api/tasks/{task_id}/logs"))
    )
    # 日志里先声明本轮上限
    assert "本次采集上限：点评 2 条" in messages, messages
    # 再确认真的只采了 2 条：这一页原本有 2 条一级 + 1 条回复 = 3 条
    assert "采集到 2 条点评" in messages, messages


def test_23_patch_while_running_says_next_round(client):
    """运行中的任务允许改，返回的提示要说清楚下一轮生效。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖携程-运行中编辑", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi",
        "schedule_type": "interval", "schedule_interval_seconds": 3600,
    }))
    task_id = task["task_id"]
    client.post(f"/api/tasks/{task_id}/run")

    # 任务很快就跑完了，所以两种情况都接受：正在跑 -> 提示下一轮；已跑完 -> 普通保存
    response = client.patch(f"/api/tasks/{task_id}", json={
        "schedule_interval_seconds": 1800,
    })
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["code"] == 0
    if body["data"]["is_running"]:
        assert "下一轮" in body["message"]

    # 等它结束，确认 reschedule 用的是改后的间隔而不是启动时那份快照
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.5)
    assert current["schedule_interval_seconds"] == 1800


# ---------------------------------------------------------------------------
# 账号 Cookie：手动导入 / 查看 / 采集
# ---------------------------------------------------------------------------

def test_24_cookie_inspect_reports_not_logged_in(client):
    _data(client.post("/api/accounts", json={
        "channel": "douyin", "account_name": "采集账号001", "nickname": "测试",
    }))

    state = _data(client.get("/api/accounts/douyin/采集账号001/cookies"))
    assert state["count"] == 0
    assert state["logged_in"] is False
    # 前端要拿这个列表告诉用户「缺什么」
    assert "sessionid" in state["expected_any_of"]
    assert state["home_url"].startswith("https://www.douyin.com")


def test_25_import_rejects_visitor_only_cookies(client):
    """只有游客 Cookie 就直接拒绝，别等到跑任务时抖音回 2483 才发现。"""
    response = client.post("/api/accounts/douyin/采集账号001/cookies", json={
        "raw": "ttwid=1%7CnSMW; passport_csrf_token=0ee9d9; "
               "passport_csrf_token_default=0ee9d9; s_v_web_id=verify_mt71",
    })
    assert response.status_code == 400
    assert "登录凭据" in response.json()["message"]

    # 被拒绝的导入不能把账号标成可用
    state = _data(client.get("/api/accounts/douyin/采集账号001/cookies"))
    assert state["logged_in"] is False


def test_26_import_header_string_cookies(client):
    raw = (
        "sessionid=679ceb84f2c9cdfb179222274a40041a;"
        "sid_tt=679ceb84f2c9cdfb179222274a40041a;"
        "uid_tt=f2e6d7021ce30a74de485c4d42053743;"
        "ttwid=1%7CnSMWL4VWA5MVTar2YBibt1pz587lv2XNoEFUXN9d5gM"
    )
    result = _data(client.post(
        "/api/accounts/douyin/采集账号001/cookies", json={"raw": raw}
    ))
    assert result["format"] == "header"
    assert result["count"] == 4
    assert result["domains"] == [".douyin.com"]

    state = _data(client.get("/api/accounts/douyin/采集账号001/cookies"))
    assert state["logged_in"] is True
    assert state["missing"] == []
    assert "sessionid" in state["names"]
    # 只回 key 不回值
    assert all("=" not in name for name in state["names"])


def test_27_import_cookie_editor_json(client):
    raw = json.dumps([
        {
            "domain": ".douyin.com", "expirationDate": 1892748274.5,
            "hostOnly": False, "httpOnly": True, "name": "sessionid",
            "path": "/", "sameSite": "no_restriction", "secure": True,
            "session": False, "value": "json_imported_session",
        },
        {
            "domain": ".douyin.com", "hostOnly": False, "httpOnly": False,
            "name": "sid_guard", "path": "/", "sameSite": "unspecified",
            "secure": False, "session": True, "value": "json_imported_guard",
        },
    ])
    result = _data(client.post(
        "/api/accounts/douyin/采集账号001/cookies", json={"raw": raw}
    ))
    assert result["format"] == "json"
    assert result["count"] == 2

    state = _data(client.get("/api/accounts/douyin/采集账号001/cookies"))
    assert state["logged_in"] is True
    assert set(state["names"]) == {"sessionid", "sid_guard"}


def test_28_import_rejects_garbage(client):
    bad = client.post("/api/accounts/douyin/采集账号001/cookies", json={"raw": "随便写点什么"})
    assert bad.status_code == 400
    assert "解析失败" in bad.json()["message"]

    empty = client.post("/api/accounts/douyin/采集账号001/cookies", json={"raw": "   "})
    assert empty.status_code == 422  # pydantic 拦下来的


def test_29_import_requires_existing_account(client):
    missing = client.post("/api/accounts/douyin/不存在的账号/cookies", json={
        "raw": "sessionid=abc",
    })
    assert missing.status_code == 404


def test_30_import_rejected_for_login_free_channels(client):
    _data(client.post("/api/accounts", json={
        "channel": "ctrip", "account_name": "携程占位",
    }))
    denied = client.post("/api/accounts/ctrip/携程占位/cookies", json={"raw": "a=1"})
    assert denied.status_code == 400
    assert "无需登录" in denied.json()["message"]


# ---------------------------------------------------------------------------
# 搜索筛选与翻页上限
# ---------------------------------------------------------------------------

def test_31_filter_options_reflect_platform_capability(client):
    """筛选表单的选项由后端给出，前端不硬编码——省得两边走偏。"""
    options = _data(client.get("/api/tasks/filter-options"))
    by_channel = {row["channel"]: row for row in options["channels"]}

    assert set(by_channel) == {"douyin", "kuaishou", "xiaohongshu", "weibo"}
    # 快手接口没有排序参数，如实反映
    assert by_channel["kuaishou"]["sort_supported"] is False
    assert by_channel["kuaishou"]["sorts"] == []
    # 但时间范围仍然可以设（本地过滤）
    assert by_channel["kuaishou"]["time_supported"] is True

    assert {o["value"] for o in by_channel["douyin"]["sorts"]} == {
        "general", "latest", "most_like",
    }
    # 小红书只有三档：参考实现（MediaCrawler / RedCrack / xhshow）里
    # 就只有 general / time_descending / popularity_descending
    assert {o["value"] for o in by_channel["xiaohongshu"]["sorts"]} == {
        "general", "latest", "most_like",
    }
    assert {o["value"] for o in options["publish_within"]} >= {
        "unlimited", "day", "week", "half_year", "custom",
    }


def test_32_task_stores_and_reports_search_filters(client):
    # 一个任务一个平台，所以这里建两条，分别断言各自的筛选摘要
    douyin_task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-带筛选-抖音", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword",
        "keywords": ["西湖"], "schedule_type": "cron", "cron_expression": "0 6 * * *",
        "params": {
            "channel_params": {"douyin": {"sort": "latest", "publish_within": "week"}},
        },
    }))
    weibo_task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-带筛选-微博", "scenic_id": "SC001",
        "channels": ["weibo"], "collect_type": "keyword",
        "keywords": ["西湖"], "schedule_type": "cron", "cron_expression": "0 6 * * *",
        "params": {
            "channel_params": {
                "weibo": {"sort": "latest", "start_date": "2026-08-01",
                          "end_date": "2026-08-27"},
            },
        },
    }))

    by_channel = {row["channel"]: row for row in
                  douyin_task["search_filters"] + weibo_task["search_filters"]}
    assert by_channel["douyin"]["sort"] == "latest"
    assert by_channel["douyin"]["publish_within"] == "week"
    assert by_channel["douyin"]["native_time_filter"] is True

    # 微博的日期区间存得下，但**不是接口侧筛的**：
    # timescope 是桌面搜索页的参数，采集走的 ajax 接口不认，
    # 所以时间范围靠客户端时间窗兜底过滤（和快手一样）
    assert by_channel["weibo"]["start_date"] == "2026-08-01"
    assert by_channel["weibo"]["native_time_filter"] is False
    assert "2026-08-01" in by_channel["weibo"]["description"]

    # 筛选项要能原样存进去、读回来
    stored_douyin = _data(client.get(f"/api/tasks/{douyin_task['task_id']}"))["params"]
    stored_weibo = _data(client.get(f"/api/tasks/{weibo_task['task_id']}"))["params"]
    assert stored_douyin["channel_params"]["douyin"]["sort"] == "latest"
    assert stored_weibo["channel_params"]["weibo"]["end_date"] == "2026-08-27"


def test_33_poi_channels_have_no_search_filters(client):
    """携程/同程没有关键字搜索，不该出现在筛选列表里。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-携程带筛选", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "cron",
        "cron_expression": "0 7 * * *",
    }))
    assert task["search_filters"] == []


def test_34_max_pages_is_configurable(client):
    """携程/同程的翻页上限以前写死 300，现在任务里能设，0 = 一直翻。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-不限页数", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "cron",
        "cron_expression": "0 8 * * *",
        "params": {"channel_params": {"ctrip": {"max_pages": 0, "max_comments": 0}}},
    }))
    ctrip = next(r for r in task["collect_limits"] if r["channel"] == "ctrip")
    assert ctrip["max_pages"] == 0, "0 要当成「不限」，不能回退成默认的 300"
    assert ctrip["max_comments"] == 0

    # 改成有限页数
    updated = _data(client.patch(f"/api/tasks/{task['task_id']}", json={
        "params": {"channel_params": {"ctrip": {"max_pages": 5}}},
    }))
    ctrip = next(r for r in updated["collect_limits"] if r["channel"] == "ctrip")
    assert ctrip["max_pages"] == 5


def test_35_max_pages_actually_caps_paging(client):
    """设了页数上限，采集就得在那一页停下。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-限两页", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "once",
        "params": {"channel_params": {"ctrip": {"max_pages": 1, "max_comments": 0}}},
    }))
    task_id = task["task_id"]
    _data(client.post(f"/api/tasks/{task_id}/run"))
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.5)
    assert current["status"] == "completed", current.get("error")

    messages = " ".join(
        log["message"] for log in _data(client.get(f"/api/tasks/{task_id}/logs"))
    )
    assert "最多翻 1 页" in messages, messages


def test_36_unlimited_pages_is_reported_as_such(client):
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-页数不限", "scenic_id": "SC001",
        "channels": ["ctrip"], "collect_type": "poi", "schedule_type": "once",
        "params": {"channel_params": {"ctrip": {"max_pages": 0}}},
    }))
    task_id = task["task_id"]
    _data(client.post(f"/api/tasks/{task_id}/run"))
    for _ in range(60):
        current = _data(client.get(f"/api/tasks/{task_id}"))
        if current["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.5)

    messages = " ".join(
        log["message"] for log in _data(client.get(f"/api/tasks/{task_id}/logs"))
    )
    assert "一直翻到没有数据" in messages, messages


# ---------------------------------------------------------------------------
# 内容过滤：页面配置 → 存进任务 → 读回来
# ---------------------------------------------------------------------------

def test_40_content_filter_is_stored_on_the_task(client):
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-内容过滤", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword", "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 8 * * *",
        "params": {
            "content_filter": {
                "enabled": True, "mode": "keyword_plus",
                # 故意混用中英文逗号和多余空格——用户就是这么填的
                "extra_keywords": "断桥残雪，雷峰塔, 苏堤 ",
            },
        },
    }))

    stored = task["params"]["content_filter"]
    assert stored["enabled"] is True
    assert stored["mode"] == "keyword_plus"
    # 存进去的是清洗过的形状：统一英文逗号、去空格
    assert stored["extra_keywords"] == "断桥残雪,雷峰塔,苏堤"

    # 读回来还是这份
    again = _data(client.get(f"/api/tasks/{task['task_id']}"))
    assert again["params"]["content_filter"] == stored


def test_41_content_filter_can_be_turned_off(client):
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-不过滤", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword", "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 9 * * *",
        "params": {"content_filter": {"enabled": False}},
    }))
    assert task["params"]["content_filter"]["enabled"] is False

    # 改回开启
    updated = _data(client.patch(f"/api/tasks/{task['task_id']}", json={
        "params": {"content_filter": {"enabled": True, "mode": "keyword"}},
    }))
    assert updated["params"]["content_filter"]["enabled"] is True


def test_42_garbage_mode_falls_back_instead_of_being_stored(client):
    """前端传了不认识的 mode，不能原样存库——
    存进去之后每次采集都要在采集器里再判一次，早晚漏一处。"""
    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-脏值", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword", "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 10 * * *",
        "params": {"content_filter": {"mode": "随便写的"}},
    }))
    assert task["params"]["content_filter"]["mode"] == "keyword"


def test_43_old_tasks_without_the_key_keep_the_old_behaviour(client):
    """老任务的 params 里没有 content_filter。

    这时必须按"开启 + 只按搜索关键字"跑——也就是改造前的行为。
    默认关掉的话，所有老任务会在升级后突然开始存广告。
    """
    from app.core.content_filter import ContentFilter

    task = _data(client.post("/api/tasks", json={
        "task_name": "西湖-老任务", "scenic_id": "SC001",
        "channels": ["douyin"], "collect_type": "keyword", "keywords": ["西湖"],
        "schedule_type": "cron", "cron_expression": "0 11 * * *",
    }))
    assert "content_filter" not in (task["params"] or {})

    rules = ContentFilter.from_params(task["params"] or {})
    assert rules.enabled is True and rules.mode == "keyword"
