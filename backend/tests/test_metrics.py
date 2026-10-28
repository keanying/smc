# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

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


def test_auth_defaults_match_the_gateway_xpftkey_without_prefix():
    """下游网关是 `xpftkey: pfk_xxx`：自定义请求头、不带 Bearer。"""
    cfg = mc.normalize({})
    assert (cfg["push"]["token_header"], cfg["push"]["token_prefix"]) == ("xpftkey", "")
    # 前缀空串 = 不加前缀，不能被当成"没填"换回默认值；Bearer 方式照样能配
    bearer = mc.normalize({"push": {"token_header": "Authorization", "token_prefix": "Bearer "}})
    assert (bearer["push"]["token_header"], bearer["push"]["token_prefix"]) == \
        ("Authorization", "Bearer ")
    assert mc.normalize({"push": {"token_header": "X-Api-Key", "token_prefix": ""}}
                        )["push"]["token_prefix"] == ""
    with pytest.raises(ValueError, match="请求头名"):
        mc.normalize({"push": {"token_header": "x pft: key"}})


def test_default_pk_follows_the_gateway_sample():
    """客户给的下钻表报文样例里的 pkId（顺序也一致）。引擎自己的默认只有 detail_uk。"""
    assert mc.DEFAULT_PUSH_PK["drill"] == ["scenic_id", "emotion_word", "emotion_type", "channel",
                                           "work_id", "comment_id", "publish_time", "travel_date"]
    assert set(mc.DEFAULT_PUSH_PK) == {t["key"] for t in mc.TABLES}


def test_engine_ddl_gives_all_six_tables_with_comments():
    ddl = mc.ddl_statements()
    assert set(ddl) == set(mc.TABLE_NAMES)
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS") for s in ddl.values())
    notes = mc.ddl_comments()["ads_trf_social_opinion_comment_core_di"]
    assert notes["columns"]["comment_count"] == "总评论数"


def test_scenic_push_overrides_which_tables_go_out():
    """按景区配推送表：配了的景区只推它自己勾的，没配的按各表的推送开关。"""
    cfg = mc.normalize({
        "tables": {"content": {"push": False}},
        "scenic_push": {"S1": ["macro", "core", "core"], "S2": []},
    })
    assert cfg["scenic_push"] == {"S1": ["core", "macro"], "S2": []}, "按固定顺序、去重"
    assert mc.push_keys_for(cfg, "S1") == ["core", "macro"]
    assert mc.push_keys_for(cfg, "S2") == [], "空列表 = 这个景区不推"
    assert mc.push_keys_for(cfg, "S9") == ["core", "platform", "dimension", "drill", "macro"]
    with pytest.raises(ValueError, match="不认识的表"):
        mc.normalize({"scenic_push": {"S1": ["nope"]}})


def test_worker_pushes_only_the_scenics_own_tables():
    pd = pytest.importorskip("pandas")
    from app.metrics import worker

    worker._load_engine()
    from engin_cli.settings import EtlSettings

    receiver = _Receiver()
    try:
        cfg = mc.normalize({"push": {"enabled": True, "url": receiver.url},
                            "scenic_push": {"S1": ["core"]}})
        frame = pd.DataFrame([{"scenic_spot_code": "S1", "publish_time": "20261010",
                               "travel_date": 20261010, "platform_code": "dy",
                               "comment_count": 1}])
        tables = {"ads_trf_social_opinion_comment_core_di": frame,
                  "ads_trf_social_opinion_comment_platform_di": frame}
        st = EtlSettings()
        st.push_log_dir = ""
        worker.push_tables(st, tables, cfg, "S1")
        assert [r["body"]["tableName"] for r in receiver.requests] == \
            ["ads_trf_social_opinion_comment_core_di"]
        receiver.requests.clear()
        worker.push_tables(st, tables, cfg, "S2")           # 没单独配：按各表开关，两张都推
        assert len(receiver.requests) == 2
    finally:
        receiver.close()


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
    assert core["body"]["keys"] == ["scenic_code", "publish_time", "travel_date"]
    assert set(core["body"]["rows"][0]) == {"scenic_code", "travel_date", "publish_time", "total"}
    drill = sent["ads_trf_social_opinion_drill_analysis_di"]
    assert drill["body"]["keys"] == mc.DEFAULT_PUSH_PK["drill"], "没配主键时用网关的默认主键"

    # 报文预览：和真正发出去的同一套规则，令牌打码
    pv = await env.manager.push_preview("core")
    assert pv["headers"]["X-Api-Key"].startswith("****") or "k-1" not in pv["headers"]["X-Api-Key"]
    assert pv["body"]["keys"] == ["scenic_code", "publish_time", "travel_date"]
    assert set(pv["body"]["rows"][0]) == {"scenic_code", "travel_date", "publish_time", "total"}
    assert pv["body"]["rows"][0]["total"] is not None, "预览用的是表里真实的最新一行"
    assert "--header 'X-Api-Key: " in pv["curl"]


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


@pytest.mark.asyncio
async def test_drill_keyword_filter_uses_only_enabled_main_and_aux_words_on_comment_text(env):
    """下钻表「只收含采集关键字的评论」：

    · 只看评论内容（不看作品标题）；词 = 启用的主关键字 + 附关键字。过滤关键字、停用的词都不算——
      引擎自带的查法把 filter_word 整张表（含过滤关键字）都当成采集关键字，含「很差」这类
      要丢的词的评论反而会进下钻表。
    · 大盘表 wordcloud_map 里的词也要写进下钻表：看板上点词云要能下钻到评论。
    """
    from app.metrics.common import yesterday

    scenics = await _seed(env.db)
    sc = scenics[0]
    await env.db.execute_many(
        "INSERT INTO src_opinion_scenic_keyword (scenic_id, keyword, enabled) VALUES (%s, %s, %s)",
        [(sc, "景色", 1), (sc, "人多", 0)])
    await env.db.execute_many(
        "INSERT INTO src_opinion_scenic_filter_word (scenic_id, kind, word, enabled) "
        "VALUES (%s, %s, %s, %s)",
        [(sc, "aux", "门票", 1), (sc, "aux", "排队", 0), (sc, "exclude", "很差", 1)])

    await env.manager.init_tables()
    await env.manager.save_config({
        "scenics": [sc],
        "params": {"lookback_days": 30, "rerun_days": 0, "drill_keyword_filter": True,
                   "drill_mask_content": False},
    })
    day = yesterday()
    run = await env.manager.start_run(start_date=day)
    done = await _wait(env.manager, run["run_id"])
    assert done["status"] == "success", (done["error"], done["log"][-1500:])
    note = next(n for n in done["summary"][0]["notes"] if n.startswith("下钻明细按关键字过滤"))
    assert "景色" in note and "门票" in note and "很差" not in note and "人多" not in note

    rows = await env.db.fetch_all(
        "SELECT content_snippet, emotion_word, emotion_type FROM "
        "ads_trf_social_opinion_drill_analysis_di WHERE scenic_id = %s", [sc])
    macro = await env.db.fetch_all(
        "SELECT wordcloud_map FROM ads_trf_social_opinion_macro_gran_metric_di "
        "WHERE scenic_id = %s AND travel_date = %s", [sc, int(day)])
    group = {"positiveWord": "positive", "neutralWord": "neutral", "negativeWord": "negative"}
    cloud = {(w["word"], group[k]) for m in macro for k, ws in json.loads(m["wordcloud_map"]).items()
             for w in ws}
    td_cloud = {(w["word"], group[k]) for m in await env.db.fetch_all(
        "SELECT wordcloud_map FROM ads_trf_social_opinion_macro_gran_metric_di "
        "WHERE scenic_id = %s AND travel_date = %s AND time_granularity = 'td'", [sc, int(day)])
        for k, ws in json.loads(m["wordcloud_map"]).items() for w in ws}
    assert rows and cloud

    bad = [r for r in rows if "景色" not in r["content_snippet"] and "门票" not in r["content_snippet"]
           and (r["emotion_word"], r["emotion_type"]) not in cloud]
    assert not bad, f"收进了既不含启用关键字、词也不在大盘词云里的行：{bad[:3]}"
    pairs = {(r["emotion_word"], r["emotion_type"]) for r in rows}
    assert td_cloud <= pairs, f"当天词云里的词在下钻表里找不到：{sorted(td_cloud - pairs)[:5]}"
    assert " 去掉 0 行" not in note, "样例里有不该收的行，过滤应该去掉一些"


@pytest.mark.asyncio
async def test_mock_copies_a_scenics_results_to_another_code_and_only_pushes(env):
    """演示数据：把一个景区算好的结果换成另一个景区编号推给下游，不写本地库。"""
    from app.metrics.common import yesterday

    scenics = await _seed(env.db)
    src, dst = scenics[0], "PFTSCA09999999"
    day = yesterday()
    await env.manager.init_tables()
    await env.manager.save_config({
        "scenics": [src], "params": {"lookback_days": 30, "rerun_days": 1},
        "push": {"enabled": False, "url": env.receiver.url},
    })
    done = await _wait(env.manager, (await env.manager.start_run(start_date=day))["run_id"])
    assert done["status"] == "success", done["error"]

    with pytest.raises(ValueError, match="一样"):
        await env.manager.start_mock(from_scenic=src, to_scenic=src, to_name="x", start_date=day,
                                     end_date=day, tables=["core"])
    check = await env.manager.mock_check(src)
    assert check["comments"] > 0 and check["ads_rows"] > 0, "目标有真实数据时要能看出来"
    assert (await env.manager.mock_check(dst))["comments"] == 0

    run = await env.manager.start_mock(from_scenic=src, to_scenic=dst, to_name="演示景区",
                                       start_date=day, end_date=day, tables=["core", "drill"])
    done = await _wait(env.manager, run["run_id"])
    assert done["status"] == "success" and done["trigger_type"] == "mock", done["error"]

    sent = {r["body"]["tableName"]: r["body"] for r in env.receiver.requests}
    assert set(sent) == {"ads_trf_social_opinion_comment_core_di",
                         "ads_trf_social_opinion_drill_analysis_di"}, "只推勾选的表（推送开关关着也推）"
    core = sent["ads_trf_social_opinion_comment_core_di"]["data"]
    assert {r["scenic_spot_code"] for r in core} == {dst}
    assert {r["scenic_spot_name"] for r in core} == {"演示景区"}
    drill = sent["ads_trf_social_opinion_drill_analysis_di"]["data"]
    src_uk = {r["detail_uk"] for r in await env.db.fetch_all(
        "SELECT detail_uk FROM ads_trf_social_opinion_drill_analysis_di WHERE scenic_id = %s", [src])}
    assert drill and not ({r["detail_uk"] for r in drill} & src_uk), "detail_uk 要按新景区重算，不能和原景区撞"
    local = await env.db.fetch_one(
        "SELECT COUNT(*) AS c FROM ads_trf_social_opinion_comment_core_di WHERE scenic_spot_code = %s",
        [dst])
    assert local["c"] == 0, "演示数据不写本地库"


def test_mock_targets_are_validated():
    ok = mc.normalize({"mock_targets": [
        {"from_scenic": "S1", "to_scenic": "D1", "to_name": "演示", "tables": ["drill", "core"]},
        {"from_scenic": "", "to_scenic": "", "to_name": ""},          # 空行忽略
    ]})
    assert ok["mock_targets"] == [{"from_scenic": "S1", "to_scenic": "D1", "to_name": "演示",
                                   "tables": ["core", "drill"], "enabled": True}]
    for bad, words in [
        ({"from_scenic": "S1", "to_scenic": "S1", "to_name": "x", "tables": ["core"]}, "一样"),
        ({"from_scenic": "S1", "to_scenic": "D1", "to_name": "x", "tables": []}, "至少选一张"),
        ({"from_scenic": "S1", "to_scenic": "D1", "to_name": "", "tables": ["core"]}, "都要填"),
    ]:
        with pytest.raises(ValueError, match=words):
            mc.normalize({"mock_targets": [bad]})
    with pytest.raises(ValueError, match="重复"):
        mc.normalize({"mock_targets": [
            {"from_scenic": "S1", "to_scenic": "D1", "to_name": "x", "tables": ["core"]},
            {"from_scenic": "S2", "to_scenic": "D1", "to_name": "y", "tables": ["core"]}]})
    with pytest.raises(ValueError, match="要计算的真实景区"):
        mc.normalize({"scenics": ["S1", "D1"], "mock_targets": [
            {"from_scenic": "S1", "to_scenic": "D1", "to_name": "x", "tables": ["core"]}]})


@pytest.mark.asyncio
async def test_demo_targets_are_pushed_right_after_their_source_scenic(env):
    """演示数据随计算：源景区推送成功后，用这次的结果换成目标编号再推；不写本地库。"""
    from app.metrics.common import yesterday

    scenics = await _seed(env.db)
    src, dst = scenics[0], "PFTSCA09999999"
    await env.manager.init_tables()
    await env.manager.save_config({
        "scenics": [src], "params": {"lookback_days": 30, "rerun_days": 1},
        "push": {"enabled": True, "url": env.receiver.url},
        "mock_targets": [{"from_scenic": src, "to_scenic": dst, "to_name": "演示景区",
                          "tables": ["core", "drill"]}],
    })
    done = await _wait(env.manager, (await env.manager.start_run(start_date=yesterday()))["run_id"])
    assert done["status"] == "success", (done["error"], done["log"][-1500:])
    assert done["summary"][0]["mock"][0]["ok"]

    order = []
    for r in env.receiver.requests:
        body = r["body"]
        code = body["data"][0].get("scenic_spot_code") or body["data"][0].get("scenic_id")
        order.append((body["tableName"], code))
    first_mock = next(i for i, (_, c) in enumerate(order) if c == dst)
    assert all(c == src for _, c in order[:first_mock]), "主景区先推完，再推演示数据"
    assert {t for t, c in order if c == dst} == {
        "ads_trf_social_opinion_comment_core_di", "ads_trf_social_opinion_drill_analysis_di"}
    dates_src = {str(x["travel_date"]) for r in env.receiver.requests
                 if r["body"]["tableName"].endswith("core_di")
                 for x in r["body"]["data"] if x["scenic_spot_code"] == src}
    dates_dst = {str(x["travel_date"]) for r in env.receiver.requests
                 if r["body"]["tableName"].endswith("core_di")
                 for x in r["body"]["data"] if x["scenic_spot_code"] == dst}
    assert dates_src == dates_dst and len(dates_dst) == 2, "日期和主景区一样，含往前重跑"
    local = await env.db.fetch_one(
        "SELECT COUNT(*) AS c FROM ads_trf_social_opinion_comment_core_di WHERE scenic_spot_code = %s",
        [dst])
    assert local["c"] == 0, "演示数据不写本地库"


@pytest.mark.asyncio
async def test_live_log_reaches_listeners_from_another_thread():
    """实时日志：子进程输出是在另一个线程（SUBPROCESS_LOOP）里写进来的，看日志的页面要一行不落地收到。"""
    import asyncio
    import threading

    from app.metrics.common import EVENT_PREFIX
    from app.metrics.manager import _RunState

    state = _RunState()
    state.feed("开始之前的一行")
    queue: asyncio.Queue = asyncio.Queue()
    snapshot = state.attach(asyncio.get_running_loop(), queue)
    assert len(snapshot) == 1 and snapshot[0].endswith("开始之前的一行"), "连上先拿到已有的"

    def worker():
        state.feed("第二行")
        state.feed(EVENT_PREFIX + '{"event": "scenic", "scenic": "S1", "ok": true, "rows": 3}')

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    got = [await asyncio.wait_for(queue.get(), 2) for _ in range(3)]
    assert got[0]["type"] == "log" and got[0]["line"].endswith("第二行")
    assert got[1]["type"] == "log"
    assert got[2]["type"] == "summary" and got[2]["summary"][0]["scenic"] == "S1"

    state.detach(queue)
    state.feed("没人看了")
    await asyncio.sleep(0.05)
    assert queue.empty(), "断开之后不再往它的队列里塞"
