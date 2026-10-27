"""指标计算（vendor/opinion_metric_engine 接入）。

客户原话："将这个项目放到这个项目中，逻辑独立，在设置页面开启指标计算，然后在菜单页就要展示
指标计算页面，定时，推送表，推送表字段，要计算的景区和参数等要能维护"。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.metrics import config as mc
from app.metrics.common import date_list

TEST_DB = "scenic_media_metrics"


# ---------------------------------------------------------------- 配置校验
def test_defaults_cover_every_table_and_push_nothing_by_default():
    cfg = mc.normalize({})
    assert set(cfg["tables"]) == {t["key"] for t in mc.TABLES}
    assert cfg["push"]["enabled"] is False and cfg["schedule"]["enabled"] is False
    assert cfg["params"]["score_weights"] == {"positive": 1.0, "neutral": 0.9, "negative": 0.5}


@pytest.mark.parametrize("raw, words", [
    ({"schedule": {"cron": "0 2 * *"}}, "5 段 cron"),
    ({"schedule": {"cron": "0 0 2 * * *"}}, "5 段 cron"),
    ({"push": {"enabled": True, "url": ""}}, "没填推送地址"),
    ({"push": {"url": "ftp://x"}}, "http"),
    ({"params": {"score_weights": {"neutral": 1.2}}}, "0~1"),
    ({"params": {"lookback_days": 0}}, "取数回溯天数"),
    ({"params": {"ads_database": "ads; drop"}}, "ADS 库名"),
    ({"tables": {"core": {"pk": ["scenic_spot_code"],
                          "fields": [{"column": "comment_count", "name": "total"}]}}}, "主键字段没有勾选"),
    ({"tables": {"core": {"fields": [{"column": "a", "name": "x"}, {"column": "b", "name": "x"}]}}},
     "重复"),
])
def test_bad_config_is_rejected_with_a_reason(raw, words):
    with pytest.raises(ValueError, match=words):
        mc.normalize(raw)


def test_push_token_is_never_echoed_and_kept_when_left_blank():
    first = mc.normalize({"push": {"url": "https://x.example/sync", "token": "sk_real"}})
    shown = mc.redact(first)
    assert shown["push"]["token"] == "" and shown["push"]["has_token"] is True
    # 页面看不到原文，原样提交回来（空 / 打码）不能把令牌清掉
    assert mc.normalize(shown, previous=first)["push"]["token"] == "sk_real"
    assert mc.normalize({**shown, "push": {**shown["push"], "token": "***"}},
                        previous=first)["push"]["token"] == "sk_real"
    assert mc.normalize({**shown, "push": {**shown["push"], "token": "sk_new"}},
                        previous=first)["push"]["token"] == "sk_new"


def test_engine_ddl_gives_all_six_tables_with_comments():
    ddl = mc.ddl_statements()
    assert set(ddl) == set(mc.TABLE_NAMES)
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS") for s in ddl.values())
    notes = mc.ddl_comments()["ads_trf_social_opinion_comment_core_di"]
    assert notes["columns"]["comment_count"] == "总评论数"


def test_date_list_includes_rerun_days():
    assert date_list("20261010", "20261011", 2) == ["20261008", "20261009", "20261010", "20261011"]
    with pytest.raises(ValueError):
        date_list("20261011", "20261001")


# ---------------------------------------------------------------- 推送字段
def test_shape_for_push_picks_renames_and_translates_pk():
    pd = pytest.importorskip("pandas")
    from app.metrics.worker import shape_for_push

    df = pd.DataFrame([{"scenic_spot_code": "S1", "travel_date": 20261010, "comment_count": 3,
                        "positive_count": 1}])
    tc = {"fields": [{"column": "scenic_spot_code", "name": "scenic_code"},
                     {"column": "travel_date", "name": "travel_date"},
                     {"column": "comment_count", "name": "total"}], "pk": []}
    out, pk = shape_for_push("t", df, tc, ["scenic_spot_code", "travel_date"])
    assert list(out.columns) == ["scenic_code", "travel_date", "total"]
    assert pk == ["scenic_code", "travel_date"]
    same, pk2 = shape_for_push("t", df, {"fields": [], "pk": []}, ["scenic_spot_code"])
    assert same is df and pk2 == ["scenic_spot_code"], "不配字段 = 全部原名"


def test_worker_reads_catalog_from_the_engine_itself():
    """字段清单以引擎为准（子进程里读），不是我们这边手抄一份。"""
    pytest.importorskip("pandas")
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-W", "error::RuntimeWarning", "-m",
                          "app.metrics.worker", "catalog"], cwd=backend, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    from app.metrics.common import EVENT_PREFIX
    event = json.loads(out.stdout.strip().splitlines()[-1][len(EVENT_PREFIX):])
    core = event["tables"]["ads_trf_social_opinion_comment_core_di"]
    assert "comment_count" in core["columns"]
    assert core["pk"] == ["scenic_spot_code", "travel_date", "publish_time"]


# ---------------------------------------------------------------- 真实 MySQL：建表 → 计算 → 推送
class _Receiver:
    """假的下游：记下收到的每个请求。"""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"headers": dict(self.headers), "body": body})
                data = json.dumps({"status": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/sync"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
async def env(tmp_path):
    pytest.importorskip("pandas")
    old = os.environ.get("SMC_MYSQL_DATABASE")
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    from app.core.config import load_config, reset_cache
    from app.core.db import Database
    from app.metrics.manager import MetricsManager
    from app.repositories.setting_repo import SettingRepository

    reset_cache()
    config = load_config(use_cache=False)
    config.set("server.data_dir", str(tmp_path))
    config.set("metrics", {"enabled": True})
    db = Database(config)
    try:
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"没有 MySQL：{exc}")
    receiver = _Receiver()
    try:
        yield SimpleNamespace(db=db, config=config, receiver=receiver,
                              manager=MetricsManager(config, db, SettingRepository(db)))
    finally:
        receiver.close()
        await db.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        await db.close()
        if old is None:
            os.environ.pop("SMC_MYSQL_DATABASE", None)
        else:
            os.environ["SMC_MYSQL_DATABASE"] = old
        reset_cache()


async def _seed(db, days: int = 12):
    """用引擎自带的样例生成器造两张源表的数据。"""
    import pandas as pd
    from app.metrics.config import ENGINE_DIR
    from app.metrics.common import yesterday

    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"metric_sample_{os.getpid()}"
    subprocess.run([sys.executable, str(ENGINE_DIR / "scripts" / "make_sample_data.py"),
                    "--days", str(days), "--end-date", yesterday(), "--out", str(out)],
                   check=True, capture_output=True, timeout=120)
    for name, table in [("comments", "src_opinion_social_work_comment_di"),
                        ("works", "src_opinion_social_work_di")]:
        df = pd.read_csv(out / f"{name}.csv", dtype=str, keep_default_na=False)
        cols = {r["COLUMN_NAME"] for r in await db.fetch_all(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
            "AND TABLE_NAME = %s AND EXTRA NOT LIKE '%%GENERATED%%'", [TEST_DB, table])} - {"id"}
        use = [c for c in df.columns if c in cols]
        rows = [tuple(v or None for v in r) for r in df[use].itertuples(index=False)]
        await db.execute_many(
            f"INSERT IGNORE INTO `{table}` ({', '.join(f'`{c}`' for c in use)}) "
            f"VALUES ({', '.join(['%s'] * len(use))})", rows)
    return sorted(set(pd.read_csv(out / "comments.csv", dtype=str)["scenic_id"]))


async def _wait(manager, run_id, timeout=240):
    for _ in range(int(timeout * 2)):
        if manager.running_run_id != run_id:
            break
        await asyncio.sleep(0.5)
    return await manager.get_run(run_id, with_log=True)


@pytest.mark.asyncio
async def test_run_writes_ads_tables_and_pushes_the_configured_fields(env):
    from app.metrics.common import yesterday

    scenics = await _seed(env.db)
    created = await env.manager.init_tables()
    assert [r["result"] for r in created] == ["已创建"] * 6
    assert {r["result"] for r in await env.manager.init_tables()} == {"已存在"}, "建表要幂等"

    await env.manager.save_config({
        "scenics": [scenics[0]],
        "params": {"lookback_days": 30, "rerun_days": 1},
        "push": {"enabled": True, "url": env.receiver.url, "token_header": "X-Api-Key",
                 "token_prefix": "", "token": "k-1",
                 "body_fields": {"table": "table_name", "pk": "keys", "data": "rows"},
                 "extra_fields": {"source": "smc"}},
        "tables": {
            "core": {"push_name": "dw_core", "fields": [
                {"column": "scenic_spot_code", "name": "scenic_code"},
                {"column": "travel_date", "name": "travel_date"},
                {"column": "publish_time", "name": "publish_time"},
                {"column": "comment_count", "name": "total"}]},
            "content": {"push": False},
        },
    })
    run = await env.manager.start_run(start_date=yesterday())
    done = await _wait(env.manager, run["run_id"])
    assert done["status"] == "success", (done["error"], done["log"][-1500:])
    assert [s["scenic"] for s in done["summary"]] == [scenics[0]], "只算配置的景区"
    assert "RuntimeWarning" not in done["log"]

    rows = await env.db.fetch_one(
        "SELECT COUNT(*) AS c, COUNT(DISTINCT travel_date) AS d "
        "FROM ads_trf_social_opinion_comment_core_di")
    assert rows["c"] >= 1 and rows["d"] == 2, "目标日期 + 往前重跑 1 天"

    sent = {r["body"]["table_name"]: r for r in env.receiver.requests}
    assert "ads_trf_social_opinion_comment_content_di" not in sent, "关掉推送的表不推"
    core = sent["dw_core"]
    assert core["headers"].get("X-Api-Key") == "k-1"
    assert core["body"]["source"] == "smc"
    assert core["body"]["keys"] == ["scenic_code", "travel_date", "publish_time"]
    assert set(core["body"]["rows"][0]) == {"scenic_code", "travel_date", "publish_time", "total"}


@pytest.mark.asyncio
async def test_run_refused_when_disabled_or_already_running(env):
    env.config.set("metrics", {"enabled": False})
    with pytest.raises(RuntimeError, match="没有开启"):
        await env.manager.start_run()
    env.config.set("metrics", {"enabled": True})
    env.manager._run_id = "mr-busy"
    with pytest.raises(RuntimeError, match="已经有一次计算在跑"):
        await env.manager.start_run()


@pytest.mark.asyncio
async def test_unfinished_runs_are_marked_failed_after_restart(env):
    from app.db import tables as T

    await env.db.execute(f"INSERT INTO `{T.METRIC_RUN}` (run_id, status) VALUES ('mr-old', 'running')")
    await env.manager.recover()
    run = await env.manager.get_run("mr-old")
    assert run["status"] == "failed" and "重启" in run["error"]
