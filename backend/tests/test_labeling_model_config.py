"""AI 标注的模型配置：改了要真生效，配错了要在自检里就拦下。

起因是线上一次事故：方舟把旧模型关了，每条评论都报
InvalidEndpoint.ClosedEndpoint、重试 3 次后进失败池。而且
  · 自检只看 Key/模型名"填没填"，模型被关了照样全绿
  · 页面上换了模型提示"已保存并立即生效"，引擎却还在打旧模型
"""
from __future__ import annotations

import asyncio
import os
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.labeling import manager as manager_mod                  # noqa: E402
from app.labeling.manager import BackfillJob, LabelingManager, probe_model  # noqa: E402

CLOSED = {"error": {
    "code": "InvalidEndpoint.ClosedEndpoint",
    "message": "The request targeted an endpoint that is currently closed "
               "or temporarily unavailable. Request id: 0217",
    "param": "", "type": "Bad Request"}}


def _llm(**kw):
    base = dict(model="glm-5-3-flash-260828", api_key="k" * 36,
                url="https://ark.example/api/v3/responses",
                connect_timeout=5.0, extra_body={})
    base.update(kw)
    return SimpleNamespace(**base)


def _transport(status, body, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- 模型连通
def test_probe_passes_on_2xx():
    ok, detail = probe_model(_llm(), transport=_transport(200, {"output": []}))
    assert ok, detail


def test_probe_sends_the_responses_shape_the_customer_uses():
    """和方舟 /responses 最小示例同形：model + input[user/input_text]。"""
    seen = []
    probe_model(_llm(extra_body={"thinking": {"type": "disabled"}}),
                transport=_transport(200, {}, seen))
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://ark.example/api/v3/responses"
    assert req.headers["Authorization"] == "Bearer " + "k" * 36
    assert body["model"] == "glm-5-3-flash-260828"
    assert body["stream"] is False
    assert body["input"][0]["content"][0]["type"] == "input_text"
    # 正式调用会带的厂商参数，自检也要带，免得自检过了正式请求被拒
    assert body["thinking"] == {"type": "disabled"}


def test_probe_explains_closed_endpoint():
    ok, detail = probe_model(_llm(model="deepseek-v4-flash-260425"),
                             transport=_transport(400, CLOSED))
    assert not ok
    assert "deepseek-v4-flash-260425" in detail
    assert "InvalidEndpoint.ClosedEndpoint" in detail
    assert "模型名称" in detail, "要告诉用户去哪儿改"


def test_probe_reports_non_json_errors():
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway"))
    ok, detail = probe_model(_llm(), transport=transport)
    assert not ok and "502" in detail and "bad gateway" in detail


def test_probe_reports_connection_errors():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    ok, detail = probe_model(_llm(), transport=httpx.MockTransport(boom))
    assert not ok and "连不上" in detail


# ---------------------------------------------------------------- 改配置生效
class _FakeEngine:
    def __init__(self):
        self.stopped = False

    def stop(self, timeout=None):
        self.stopped = True


def test_reset_drops_engine_and_degraded_flag():
    mgr = LabelingManager(SimpleNamespace(get=lambda *a, **k: {}))
    engine = _FakeEngine()
    mgr._engine, mgr._degraded, mgr._start_error = engine, True, "模型连通：ClosedEndpoint"

    asyncio.run(mgr.reset())

    assert engine.stopped
    assert mgr._engine is None, "旧引擎还在，就还在打旧模型"
    assert mgr._degraded is False, "降级标记不清，改好了也不会再试"
    assert mgr._start_error == ""


def test_reset_cancels_running_backfill_with_a_reason():
    mgr = LabelingManager(SimpleNamespace(get=lambda *a, **k: {}))
    running = BackfillJob(job_id="a", status="running")
    done = BackfillJob(job_id="b", status="finished")
    mgr._jobs = {"a": running, "b": done}

    asyncio.run(mgr.reset())

    assert running.cancel_requested and "配置已修改" in running.error
    assert not done.cancel_requested


def test_saving_labeling_settings_resets_the_engine(monkeypatch):
    from app.api import settings as settings_api
    from app.api.schemas import SettingIn

    calls = []

    class _Mgr:
        async def reset(self):
            calls.append("reset")

    async def _save(section, values, config):
        return None

    monkeypatch.setattr("app.labeling.get_manager", lambda config=None: _Mgr())
    state = SimpleNamespace(
        settings=SimpleNamespace(save_section=_save),
        config=SimpleNamespace(redacted=lambda: {}),
        proxy_manager=SimpleNamespace(reset=lambda: calls.append("proxy")),
    )
    asyncio.run(settings_api.save_settings(
        SettingIn(section="labeling", values={"ark": {"model": "glm-5-3-flash-260828"}}),
        state))
    assert calls == ["reset"]

    calls.clear()
    asyncio.run(settings_api.save_settings(
        SettingIn(section="crawl", values={"max_retries": 3}), state))
    assert calls == [], "改别的设置不该把标注引擎停掉"


def test_disabled_manager_has_reset():
    """服务还没起来时 get_manager() 给的空壳也要能 reset，别在保存设置时炸。"""
    asyncio.run(manager_mod._DISABLED.reset())


# ---------------------------------------------------------------- 重启续标
class _Queue:
    def __init__(self, backlog):
        self._n = backlog

    def size(self):
        return self._n

    def inflight(self):
        return 0


class _Repo:
    def __init__(self, rows):
        self.rows, self.where = rows, None

    def fetch_unlabeled(self, limit=100, extra_where=""):
        self.where, self.limit = extra_where, limit
        return list(self.rows)


class _Engine:
    def __init__(self, backend, backlog=0, rows=()):
        self.cfg = SimpleNamespace(
            queue=SimpleNamespace(backend=backend),
            storage=SimpleNamespace(review_column="label_review_flag", pending_flags=[0]))
        self.queue = _Queue(backlog)
        self.repository = _Repo(rows)
        self.submitted = []

    def submit_many(self, rows):
        self.submitted.extend(rows)
        return len(rows)


def _enabled_mgr(engine=None, start_ok=True):
    mgr = LabelingManager(SimpleNamespace(
        get=lambda key, default=None: {"enabled": True} if key == "labeling" else default))

    async def _start():
        if start_ok:
            mgr._engine = engine
        else:
            mgr._start_error = "模型连通：ClosedEndpoint"
        return start_ok

    mgr.ensure_started = _start
    return mgr


def test_resume_does_nothing_when_labeling_is_off():
    mgr = LabelingManager(SimpleNamespace(get=lambda *a, **k: {}))
    assert asyncio.run(mgr.resume_pending()) == ""


def test_resume_starts_engine_for_redis_backlog_without_resubmitting():
    """客户用的是 redis 队列：重启后剩下的评论还在 Redis 里，拉起引擎就会接着消费。
    再从库里补投一遍会重复标、重复花钱。"""
    engine = _Engine("redis", backlog=37)
    msg = asyncio.run(_enabled_mgr(engine).resume_pending())
    assert "37" in msg
    assert engine.submitted == [], "redis 队列不该补投"
    assert engine.repository.where is None


def test_resume_refills_memory_queue_from_recent_unlabeled_rows():
    rows = [{"comment_id": "c1"}, {"comment_id": "c2"}]
    engine = _Engine("memory", rows=rows)
    msg = asyncio.run(_enabled_mgr(engine).resume_pending())
    assert engine.submitted == rows and "2" in msg
    where = engine.repository.where
    assert "`crawl_time` >=" in where, "只补最近的，不把全部历史都标一遍"
    assert "`label_review_flag` IN (0)" in where, "失败过的(5)走重标，不在这里重试"
    assert engine.repository.limit == LabelingManager.RESUME_MAX_ROWS


def test_resume_reports_instead_of_crashing_when_engine_cannot_start():
    msg = asyncio.run(_enabled_mgr(start_ok=False).resume_pending())
    assert "ClosedEndpoint" in msg


def test_reset_kicks_off_resume_when_enabled():
    """改完配置（比如换了模型）不能等下一次采集：队列里压着的要立刻按新配置接着标。"""
    engine = _Engine("redis", backlog=5)
    mgr = _enabled_mgr(engine)
    called = []
    orig = mgr.resume_pending

    async def _spy():
        called.append(1)
        return await orig()

    mgr.resume_pending = _spy

    async def _go():
        await mgr.reset()
        await asyncio.sleep(0.05)       # 让后台任务跑起来
    asyncio.run(_go())
    assert called == [1]


def test_startup_resumes_labeling():
    """服务启动时就要续标，不能再"懒启动"等下一次采集任务。"""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert '_resume_quietly("服务启动")' in text


def test_recent_pending_where_is_valid_sql():
    """extra_where 是拼出来的字符串，必须在真库上能跑。"""
    import pymysql

    mgr = LabelingManager(SimpleNamespace(get=lambda *a, **k: {}))
    where = mgr._recent_pending_where(SimpleNamespace(
        review_column="label_review_flag", pending_flags=[0]))
    try:
        conn = pymysql.connect(host=os.environ.get("SMC_MYSQL_HOST", "127.0.0.1"),
                               user=os.environ.get("SMC_MYSQL_USER", "root"),
                               password=os.environ.get("SMC_MYSQL_PASSWORD", ""))
    except Exception as exc:  # noqa: BLE001
        import pytest
        pytest.skip(f"没有 MySQL：{exc}")
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS smc_where_check")
        cur.execute("USE smc_where_check")
        cur.execute("CREATE TABLE IF NOT EXISTS t (crawl_time DATETIME, "
                    "label_review_flag TINYINT NULL, sentiment_label VARCHAR(20))")
        cur.execute("DELETE FROM t")
        cur.execute("INSERT INTO t VALUES (NOW(), 0, NULL), (NOW(), NULL, NULL), "
                    "(NOW(), 5, NULL), (NOW() - INTERVAL 3 DAY, 0, NULL)")
        cur.execute(f"SELECT COUNT(*) FROM t WHERE {where}")
        assert cur.fetchone()[0] == 2, "只要最近的、0 或 NULL 的"
        cur.execute("DROP DATABASE smc_where_check")
    conn.close()
