"""跑批脚本单测。

没有真 MySQL，用 sqlite 装一个说同样协议的假连接池：
    - ``%s`` 占位符翻译成 ``?``
    - 游标返回 dict 行（对齐 pymysql 的 DictCursor）
sqlite 认反引号标识符，所以脚本拼出来的 SQL 可以原样跑，
能真正验证到 WHERE 条件、游标翻页、抽干逻辑，而不只是字符串比对。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import label_from_table as lft                                    # noqa: E402
from opinion_labeling_engine.config import load_config            # noqa: E402

TABLE = "src_opinion_social_work_comment_di"


# ===========================================================================
# sqlite 假连接池
# ===========================================================================
class _Cursor:
    def __init__(self, raw: sqlite3.Cursor) -> None:
        self._raw = raw

    def execute(self, sql: str, params=()) -> None:
        self._raw.execute(sql.replace("%s", "?"), tuple(params))

    def executemany(self, sql: str, seq) -> int:
        self._raw.executemany(sql.replace("%s", "?"), [tuple(p) for p in seq])
        return self._raw.rowcount

    def fetchone(self):
        row = self._raw.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._raw.fetchall()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._raw.close()


class _Conn:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    def cursor(self) -> _Cursor:
        return _Cursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()


class SqlitePool:
    """与 MySQLPool 同协议的假池。"""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"""
            CREATE TABLE `{TABLE}` (
                channel TEXT, work_id TEXT, scenic_id TEXT, scenic_name TEXT,
                comment_id TEXT, content TEXT, publish_time TEXT,
                sentiment_label TEXT, sentiment_score INTEGER,
                dimension_tags TEXT, entity_tags TEXT, keyword_tags TEXT,
                label_review_flag INTEGER NOT NULL DEFAULT 0
            )
        """)
        for row in rows:
            cols = ", ".join(row)
            marks = ", ".join("?" * len(row))
            self._db.execute(f"INSERT INTO `{TABLE}` ({cols}) VALUES ({marks})",
                             tuple(row.values()))
        self._db.commit()

    @contextmanager
    def connection(self):
        yield _Conn(self._db)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass

    # 测试辅助
    def mark_labeled(self, comment_ids, *, label: str = "中性", score: int = 0,
                     flag: int = 4) -> None:
        marks = ", ".join("?" * len(comment_ids))
        self._db.execute(
            f"UPDATE `{TABLE}` SET sentiment_label=?, sentiment_score=?, "
            f"label_review_flag=? WHERE comment_id IN ({marks})",
            (label, score, flag, *comment_ids))
        self._db.commit()

    def set_flag(self, comment_ids, flag: int) -> None:
        marks = ", ".join("?" * len(comment_ids))
        self._db.execute(
            f"UPDATE `{TABLE}` SET label_review_flag=? WHERE comment_id IN ({marks})",
            (flag, *comment_ids))
        self._db.commit()

    def labeled_count(self) -> int:
        cur = self._db.execute(
            f"SELECT COUNT(*) FROM `{TABLE}` "
            f"WHERE sentiment_label IS NOT NULL AND sentiment_label <> ''")
        return cur.fetchone()[0]


# ===========================================================================
# 夹具
# ===========================================================================
def make_rows(n: int = 6) -> List[Dict[str, Any]]:
    out = []
    for i in range(1, n + 1):
        out.append({
            "channel": "ctrip" if i % 2 else "xhs",
            "work_id": f"w{i}",
            "scenic_id": "PFTSCA01009835" if i <= 4 else "PFTSCA01000001",
            "scenic_name": "天山天池" if i <= 4 else "瑶琳仙境",
            "comment_id": f"c{i:03d}",
            "content": f"第{i}条评论，风景不错",
            "publish_time": f"2026-08-{20 + i:02d} 10:00:00",
        })
    return out


def make_args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        config=None, mode="pending", scenic_id=[], channel=[], since=None, until=None,
        where="", skip_empty_content=False, limit=0, order_by="comment_id",
        include_reviewed=False, dedupe=True, pending_flags=None,
        resume_from=None, batch_size=100, queue_high_water=2000, drain_timeout=30.0,
        stall_timeout=2.0,
        dry_run=False, sync=False, out=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture()
def cfg():
    return load_config(require_db=False)


def make_source(cfg, rows, monkeypatch, **kwargs) -> lft.CommentSource:
    pool = SqlitePool(rows)
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    source = lft.CommentSource(cfg, make_args(**kwargs))
    source.pool = pool          # 给测试留个句柄
    return source


# ===========================================================================
# 取数
# ===========================================================================
def test_selects_exactly_the_six_contract_fields(cfg, monkeypatch):
    source = make_source(cfg, make_rows(3), monkeypatch)
    batch = source.fetch_batch(10)
    assert len(batch) == 3
    assert set(batch[0]) == {"channel", "work_id", "scenic_id",
                             "scenic_name", "comment_id", "content"}


def test_pending_mode_only_returns_unlabeled(cfg, monkeypatch):
    source = make_source(cfg, make_rows(6), monkeypatch)
    source.pool.mark_labeled(["c001", "c002"])
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c003", "c004", "c005", "c006"]


def test_all_mode_returns_labeled_rows_too(cfg, monkeypatch):
    source = make_source(cfg, make_rows(4), monkeypatch, mode="all")
    source.pool.mark_labeled(["c001", "c002"])
    assert len(source.fetch_batch(10)) == 4


def test_count_respects_filters(cfg, monkeypatch):
    source = make_source(cfg, make_rows(6), monkeypatch,
                         scenic_id=["PFTSCA01009835"])
    assert source.count() == 4


def test_channel_filter_accepts_multiple(cfg, monkeypatch):
    source = make_source(cfg, make_rows(6), monkeypatch, channel=["ctrip"])
    assert all(r["channel"] == "ctrip" for r in source.fetch_batch(10))
    assert source.count() == 3


def test_time_range_filter(cfg, monkeypatch):
    source = make_source(cfg, make_rows(6), monkeypatch,
                         since="2026-08-23", until="2026-08-26")
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c003", "c004", "c005"]


def test_extra_where(cfg, monkeypatch):
    source = make_source(cfg, make_rows(6), monkeypatch,
                         where="comment_id = 'c005'")
    assert [r["comment_id"] for r in source.fetch_batch(10)] == ["c005"]


def test_empty_content_included_by_default(cfg, monkeypatch):
    """空内容默认要捞出来标成中性，不能留 NULL。"""
    rows = make_rows(2) + [{
        "channel": "weibo", "work_id": "w9", "scenic_id": "PFTSCA01009835",
        "scenic_name": "天山天池", "comment_id": "c009", "content": "",
        "publish_time": "2026-08-29 10:00:00",
    }]
    source = make_source(cfg, rows, monkeypatch)
    assert "c009" in [r["comment_id"] for r in source.fetch_batch(10)]

    source2 = make_source(cfg, rows, monkeypatch, skip_empty_content=True)
    assert "c009" not in [r["comment_id"] for r in source2.fetch_batch(10)]


# ===========================================================================
# 游标翻页
# ===========================================================================
def test_all_mode_cursor_pagination(cfg, monkeypatch):
    source = make_source(cfg, make_rows(5), monkeypatch, mode="all")

    first = source.fetch_batch(2)
    assert [r["comment_id"] for r in first] == ["c001", "c002"]
    assert source.cursor_value == "c002"

    second = source.fetch_batch(2)
    assert [r["comment_id"] for r in second] == ["c003", "c004"]

    third = source.fetch_batch(2)
    assert [r["comment_id"] for r in third] == ["c005"]
    assert source.fetch_batch(2) == []


def test_resume_from_skips_processed(cfg, monkeypatch):
    source = make_source(cfg, make_rows(5), monkeypatch,
                         mode="all", resume_from="c003")
    assert [r["comment_id"] for r in source.fetch_batch(10)] == ["c004", "c005"]


def test_cursor_column_outside_contract_is_selected_but_not_returned_to_engine(
        cfg, monkeypatch):
    """用 publish_time 当游标时要额外 SELECT 它，否则翻页取不到游标值。"""
    source = make_source(cfg, make_rows(3), monkeypatch,
                         mode="all", order_by="publish_time")
    batch = source.fetch_batch(2)
    assert "publish_time" in batch[0]
    assert source.cursor_value == "2026-08-22 10:00:00"


# ===========================================================================
# 抽干循环
# ===========================================================================
class StubEngine:
    """只实现 Runner 用到的那几个方法。"""

    def __init__(self, pool: SqlitePool, *, accept_all: bool = True) -> None:
        self._pool = pool
        self._accept_all = accept_all
        self.submitted: List[str] = []
        self.queue = type("Q", (), {"stats": staticmethod(
            lambda: {"pending": 0, "inflight": 0, "dead": 0})})()

    def submit_many(self, rows) -> int:
        if not self._accept_all:
            return 0
        ids = [r["comment_id"] for r in rows]
        self.submitted.extend(ids)
        self._pool.mark_labeled(ids, label="正向", score=1, flag=4)   # 模拟 worker 写回
        return len(ids)

    def wait_idle(self, timeout=None) -> bool:
        return True

    def stats(self):
        return {"processed": len(self.submitted), "pending": 0, "inflight": 0,
                "dead": 0, "submitted": len(self.submitted), "total": len(self.submitted),
                "labeled": len(self.submitted), "skipped_invalid": 0, "irrelevant": 0,
                "parse_repaired": 0, "failed": 0, "low_confidence": 0,
                "dropped_on_submit": 0}


def build_runner(cfg, rows, monkeypatch, *, accept_all=True, **kwargs) -> lft.Runner:
    pool = SqlitePool(rows)
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    monkeypatch.setattr(lft.LabelingEngine, "from_config",
                        classmethod(lambda cls, *a, **kw: StubEngine(pool, accept_all=accept_all)))
    runner = lft.Runner(cfg, make_args(**kwargs))
    runner.pool = pool
    return runner


def test_pending_drain_loop_covers_every_row(cfg, monkeypatch):
    """批次比总量小，抽干循环要把表抽空且不重复。"""
    runner = build_runner(cfg, make_rows(7), monkeypatch, batch_size=3)
    runner._loop(total=7)
    assert runner._engine.submitted == [f"c{i:03d}" for i in range(1, 8)]
    assert runner.pool.labeled_count() == 7


def test_limit_stops_early(cfg, monkeypatch):
    runner = build_runner(cfg, make_rows(10), monkeypatch, batch_size=3, limit=5)
    runner._loop(total=10)
    assert len(runner._engine.submitted) == 5
    assert runner.pool.labeled_count() == 5


def test_all_rejected_batches_do_not_loop_forever(cfg, monkeypatch):
    """整批入队失败（比如全缺主键）时必须停下来，不能死循环。"""
    runner = build_runner(cfg, make_rows(5), monkeypatch,
                          batch_size=2, accept_all=False)
    runner._loop(total=5)
    assert runner._engine.submitted == []
    assert runner.pool.labeled_count() == 0


def test_stop_flag_breaks_the_loop(cfg, monkeypatch):
    runner = build_runner(cfg, make_rows(10), monkeypatch, batch_size=2)
    runner._stopping = True
    runner._loop(total=10)
    assert runner._engine.submitted == []


def test_all_mode_loop_paginates_without_waiting(cfg, monkeypatch):
    runner = build_runner(cfg, make_rows(5), monkeypatch, mode="all", batch_size=2)
    runner._loop(total=5)
    assert runner._engine.submitted == [f"c{i:03d}" for i in range(1, 6)]


# ===========================================================================
# CLI 参数校验
# ===========================================================================
def test_out_without_dry_run_is_rejected():
    assert lft.main(["--out", "x.csv"]) == 1


def test_sync_without_dry_run_is_rejected():
    assert lft.main(["--sync"]) == 1


def test_duration_format():
    assert lft._fmt_duration(45) == "45秒"
    assert lft._fmt_duration(125) == "2分5秒"
    assert lft._fmt_duration(7300) == "2小时1分"


# ===========================================================================
# 标注状态与重标模式（label_review_flag）
# ===========================================================================
def test_pending_skips_already_labeled_rows(cfg, monkeypatch):
    """已经标注过的行默认绝不重标。"""
    source = make_source(cfg, make_rows(6), monkeypatch)
    source.pool.mark_labeled(["c001", "c002", "c003"], label="正向", score=1, flag=4)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c004", "c005", "c006"]
    assert source.count() == 3


def test_failed_mode_picks_only_ai_failed_rows(cfg, monkeypatch):
    """--mode failed 只捞 label_review_flag=5 的行。"""
    source = make_source(cfg, make_rows(6), monkeypatch, mode="failed")
    source.pool.set_flag(["c002", "c005"], 5)      # AI 标注错误
    source.pool.set_flag(["c003"], 4)              # AI 标注成功
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c002", "c005"]


def test_failed_rows_are_not_picked_up_by_pending(cfg, monkeypatch):
    """失败的行标记是 5，不再被 pending 当成"未标注"反复捞。"""
    source = make_source(cfg, make_rows(4), monkeypatch)
    source.pool.set_flag(["c001", "c002"], 5)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c003", "c004"]


def test_neutral_mode_picks_neutral_rows(cfg, monkeypatch):
    """--mode neutral 重标中性行，用来清理历史兜底写进去的假中性。"""
    source = make_source(cfg, make_rows(6), monkeypatch, mode="neutral")
    source.pool.mark_labeled(["c001", "c002"], label="中性", score=0, flag=4)
    source.pool.mark_labeled(["c003"], label="正向", score=1, flag=4)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c001", "c002"]


def test_neutral_mode_leaves_human_reviewed_rows_alone(cfg, monkeypatch):
    """人工复核过的（1/2/3）不该被 AI 覆盖。"""
    rows = make_rows(5)
    source = make_source(cfg, rows, monkeypatch, mode="neutral")
    source.pool.mark_labeled(["c001"], label="中性", score=0, flag=4)   # AI 标的
    source.pool.mark_labeled(["c002"], label="中性", score=0, flag=1)   # 人工复核正确
    source.pool.mark_labeled(["c003"], label="中性", score=0, flag=2)   # 人工复核错误
    source.pool.mark_labeled(["c004"], label="中性", score=0, flag=3)   # 复核成功
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c001"]


def test_include_reviewed_overrides_that(cfg, monkeypatch):
    source = make_source(cfg, make_rows(3), monkeypatch,
                         mode="neutral", include_reviewed=True)
    source.pool.mark_labeled(["c001"], label="中性", score=0, flag=1)
    source.pool.mark_labeled(["c002"], label="中性", score=0, flag=4)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert set(ids) >= {"c001", "c002"}


def test_neutral_drain_loop_terminates(cfg, monkeypatch):
    """中性行被重标成正向后就不再匹配条件，抽干循环能正常收敛。"""
    runner = build_runner(cfg, make_rows(5), monkeypatch, mode="neutral", batch_size=2)
    runner.pool.mark_labeled([f"c{i:03d}" for i in range(1, 6)],
                             label="中性", score=0, flag=4)
    runner._loop(total=5)
    assert sorted(runner._engine.submitted) == [f"c{i:03d}" for i in range(1, 6)]


# ===========================================================================
# 重复行去重
# ===========================================================================
def _dup_rows():
    """同一 (channel, scenic_id, comment_id) 出现三次，外加两条正常行。"""
    base = {"channel": "ctrip", "work_id": "w1", "scenic_id": "S1",
            "scenic_name": "天山天池", "comment_id": "dup-1",
            "content": "风景不错", "publish_time": "2026-08-21 10:00:00"}
    rows = [dict(base), dict(base, work_id="w2"), dict(base, work_id="w3")]
    rows.append(dict(base, comment_id="ok-1", content="另一条"))
    rows.append(dict(base, comment_id="ok-2", content="又一条"))
    return rows


def test_duplicate_key_rows_are_deduped(cfg, monkeypatch):
    """同键重复行只标一次——一次 UPDATE 会把它们全部更新。"""
    source = make_source(cfg, _dup_rows(), monkeypatch)
    batch = source.fetch_batch(10)
    assert [r["comment_id"] for r in batch] == ["dup-1", "ok-1", "ok-2"]
    assert source.duplicates_skipped == 2


def test_dedupe_can_be_turned_off(cfg, monkeypatch):
    source = make_source(cfg, _dup_rows(), monkeypatch, dedupe=False)
    assert len(source.fetch_batch(10)) == 5
    assert source.duplicates_skipped == 0


def test_dedupe_uses_the_full_write_back_key(cfg, monkeypatch):
    """comment_id 相同但渠道不同 → 是两行不同数据，不能去重。"""
    rows = [
        {"channel": "ctrip", "work_id": "w1", "scenic_id": "S1", "scenic_name": "A",
         "comment_id": "same", "content": "内容一", "publish_time": "2026-08-21 10:00:00"},
        {"channel": "xhs", "work_id": "w2", "scenic_id": "S1", "scenic_name": "A",
         "comment_id": "same", "content": "内容二", "publish_time": "2026-08-21 10:00:00"},
    ]
    source = make_source(cfg, rows, monkeypatch)
    assert len(source.fetch_batch(10)) == 2
    assert source.duplicates_skipped == 0


def test_dedupe_keeps_the_drain_loop_correct(cfg, monkeypatch):
    """去重之后抽干循环仍要覆盖所有不同的键，且能正常收敛。"""
    runner = build_runner(cfg, _dup_rows(), monkeypatch, batch_size=2)
    runner._loop(total=5)
    assert sorted(set(runner._engine.submitted)) == ["dup-1", "ok-1", "ok-2"]


# ===========================================================================
# 取数没有推进（会烧钱的死循环）
# ===========================================================================
class StuckEngine(StubEngine):
    """模拟"标了但写不回去"：接收提交，但不改数据库状态。"""

    def submit_many(self, rows) -> int:
        ids = [r["comment_id"] for r in rows]
        self.submitted.extend(ids)          # 花了模型调用
        return len(ids)                     # 但一行都没写回去


def build_stuck_runner(cfg, rows, monkeypatch, **kwargs) -> lft.Runner:
    pool = SqlitePool(rows)
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    monkeypatch.setattr(lft.LabelingEngine, "from_config",
                        classmethod(lambda cls, *a, **kw: StuckEngine(pool)))
    runner = lft.Runner(cfg, make_args(**kwargs))
    runner.pool = pool
    return runner


def test_rows_that_never_write_back_do_not_loop_forever(cfg, monkeypatch):
    """写不回去的行会一直匹配取数条件。不识别出来就是无限重标、无限烧钱。"""
    runner = build_stuck_runner(cfg, make_rows(6), monkeypatch, batch_size=3)
    runner._loop(total=6)

    # 每条最多被提交一次，绝不能出现同一条被标好几轮
    assert len(runner._engine.submitted) == len(set(runner._engine.submitted))
    assert runner._source.repeats_across_batches > 0


def test_stuck_run_stops_instead_of_spinning(cfg, monkeypatch, caplog):
    runner = build_stuck_runner(cfg, make_rows(4), monkeypatch, batch_size=4)
    with caplog.at_level("ERROR"):
        runner._loop(total=4)
    assert "取数没有推进" in caplog.text
    assert "写回主键" in caplog.text


def test_normal_run_reports_no_repeats(cfg, monkeypatch):
    """正常写回的跑批不该误报跨批重复。"""
    runner = build_runner(cfg, make_rows(6), monkeypatch, batch_size=2)
    runner._loop(total=6)
    assert runner._source.repeats_across_batches == 0
    assert sorted(runner._engine.submitted) == [f"c{i:03d}" for i in range(1, 7)]


# ===========================================================================
# pending_flags
# ===========================================================================
def test_pending_flags_default_is_zero_only(cfg, monkeypatch):
    source = make_source(cfg, make_rows(4), monkeypatch)
    source.pool.set_flag(["c001"], 4)
    source.pool.set_flag(["c002"], 5)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c003", "c004"]
    assert source.use_cursor is False


def test_pending_flags_can_include_ai_success(cfg, monkeypatch):
    """--pending-flags 0 4：连 AI 标过的一起重标。"""
    source = make_source(cfg, make_rows(4), monkeypatch, pending_flags=[0, 4])
    source.pool.mark_labeled(["c001"], label="正向", score=1, flag=4)
    source.pool.set_flag(["c002"], 5)
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert "c001" in ids          # flag=4 被纳入
    assert "c002" not in ids      # flag=5 走 --mode failed
    assert "c003" in ids and "c004" in ids


def test_including_engine_written_flag_switches_to_cursor(cfg, monkeypatch, caplog):
    """把 4 放进待标注集合后，取数条件不再收缩 —— 必须自动切游标翻页，否则死循环。"""
    with caplog.at_level("WARNING"):
        source = make_source(cfg, make_rows(4), monkeypatch, pending_flags=[0, 4])
    assert source.use_cursor is True
    assert "无限重标" in caplog.text


def test_cursor_mode_paginates_and_terminates(cfg, monkeypatch):
    source = make_source(cfg, make_rows(5), monkeypatch, pending_flags=[0, 4])
    first = [r["comment_id"] for r in source.fetch_batch(2)]
    second = [r["comment_id"] for r in source.fetch_batch(2)]
    third = [r["comment_id"] for r in source.fetch_batch(2)]
    assert first == ["c001", "c002"]
    assert second == ["c003", "c004"]
    assert third == ["c005"]
    assert source.fetch_batch(2) == []


def test_flag_zero_rows_that_were_labeled_before_the_column_existed(cfg, monkeypatch):
    """标记列是后加的：历史上标过、标记仍是 0 的行不能被当成"没标过"。"""
    source = make_source(cfg, make_rows(4), monkeypatch)
    # 有标注结果，但标记还是 0（加列之前标的）
    source.pool._db.execute(
        f"UPDATE `{TABLE}` SET sentiment_label='正向', sentiment_score=1, "
        f"label_review_flag=0 WHERE comment_id IN ('c001','c002')")
    source.pool._db.commit()
    ids = [r["comment_id"] for r in source.fetch_batch(10)]
    assert ids == ["c003", "c004"]


# ===========================================================================
# --diagnose 体检
# ===========================================================================
def _diag(cfg, rows, monkeypatch, capsys, **kwargs):
    pool = SqlitePool(rows)
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    monkeypatch.setattr(lft.CommentSource, "close", lambda self: None)
    rc = lft.diagnose(cfg, make_args(**kwargs))
    return rc, capsys.readouterr().out, pool


def test_diagnose_counts_rows_and_distinct_keys(cfg, monkeypatch, capsys):
    rows = make_rows(5)
    rows.append(dict(rows[0]))              # 重复主键
    rc, out, _ = _diag(cfg, rows, monkeypatch, capsys)
    assert rc == 0
    assert "总行数            6" in out
    assert "不同主键数        5" in out
    assert "有 1 行是重复主键" in out


def test_diagnose_reports_flag_distribution(cfg, monkeypatch, capsys):
    pool = SqlitePool(make_rows(4))
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    monkeypatch.setattr(lft.CommentSource, "close", lambda self: None)
    pool.mark_labeled(["c001"], label="正向", score=1, flag=4)
    pool.set_flag(["c002"], 5)
    lft.diagnose(cfg, make_args())
    out = capsys.readouterr().out
    assert "AI标注成功" in out and "AI标注错误" in out
    assert "待标注    2 条" in out


def test_diagnose_flags_a_broken_write_back_key(cfg, monkeypatch, capsys):
    """主键值取出来又查不回去 —— 这正是"标了写不进去"的根因，体检必须抓到。"""
    rows = make_rows(2)
    pool = SqlitePool(rows)
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    monkeypatch.setattr(lft.CommentSource, "close", lambda self: None)
    # 模拟取数拿到的值和库里存的对不上（尾部空格）
    original = lft.CommentSource.fetch_batch

    def padded(self, size):
        out = original(self, size)
        for row in out:
            row["comment_id"] = row["comment_id"] + " "
        return out

    monkeypatch.setattr(lft.CommentSource, "fetch_batch", padded)
    rc = lft.diagnose(cfg, make_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "写回必然失败" in out
    assert "NO PAD" in out


# ===========================================================================
# 收尾：不能因为超时就把队列里没处理的任务丢掉
# ===========================================================================
class SlowEngine(StubEngine):
    """模拟"处理得慢但一直在推进"：每次查状态都多完成一条。"""

    def __init__(self, pool, total_to_finish: int) -> None:
        super().__init__(pool)
        self._remaining = total_to_finish
        self._done = 0
        self.idle_calls = 0

    def wait_idle(self, timeout=None) -> bool:
        self.idle_calls += 1
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        self._done += 1
        return False

    def stats(self):
        s = super().stats()
        s.update({"processed": self._done, "pending": max(0, self._remaining)})
        return s


def test_drain_waits_as_long_as_progress_continues(cfg, monkeypatch):
    """只要还在完成，就不能停——固定超时会把几万条队列任务丢掉。"""
    pool = SqlitePool(make_rows(1))
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)
    engine = SlowEngine(pool, total_to_finish=25)
    monkeypatch.setattr(lft.LabelingEngine, "from_config",
                        classmethod(lambda cls, *a, **kw: engine))
    runner = lft.Runner(cfg, make_args(stall_timeout=5.0))

    assert runner._wait_until_done("测试") is True
    assert engine.idle_calls >= 25          # 一直等到真的做完
    assert runner._stalled is False


def test_drain_gives_up_only_when_nothing_progresses(cfg, monkeypatch, caplog):
    """卡住不动才放弃，并且要说清楚剩多少条没标。"""
    pool = SqlitePool(make_rows(1))
    monkeypatch.setattr(lft, "MySQLPool", lambda _cfg: pool)

    class Frozen(StubEngine):
        def wait_idle(self, timeout=None):
            return False

        def stats(self):
            s = super().stats()
            s.update({"processed": 7, "pending": 5000})
            return s

    engine = Frozen(pool)
    monkeypatch.setattr(lft.LabelingEngine, "from_config",
                        classmethod(lambda cls, *a, **kw: engine))
    runner = lft.Runner(cfg, make_args(stall_timeout=0.1))

    with caplog.at_level("ERROR"):
        assert runner._wait_until_done("测试") is False
    assert runner._stalled is True
    assert "5000" in caplog.text
    assert "重跑一次会接着标" in caplog.text
