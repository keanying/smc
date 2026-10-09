"""历史数据修正脚本单测。

这个脚本要动的是**已经在库里的生产数据**，所以它比别的脚本更需要单测盯着：
改多了会把真抱怨抹掉，改少了拥挤度指标继续偏高，
覆盖到人工复核过的行更是直接把人的结论盖掉。

和 test_label_from_table 一样，用 sqlite 装一个同协议的假连接池，
脚本拼出来的 SQL 原样执行——验的是真 SQL 行为，不是字符串比对。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fix_inquiry_labels as fix                                  # noqa: E402
from opinion_labeling_engine.config import load_config            # noqa: E402

TABLE = "src_opinion_social_work_comment_di"


# =========================================================== sqlite 假连接池
class _Cursor:
    def __init__(self, raw: sqlite3.Cursor) -> None:
        self._raw = raw

    def execute(self, sql: str, params=()) -> None:
        self._raw.execute(sql.replace("%s", "?"), tuple(params))

    def executemany(self, sql: str, seq) -> int:
        self._raw.executemany(sql.replace("%s", "?"), [tuple(p) for p in seq])
        return self._raw.rowcount

    @property
    def rowcount(self) -> int:
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
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"""
            CREATE TABLE `{TABLE}` (
                channel TEXT, work_id TEXT, scenic_id TEXT, scenic_name TEXT,
                comment_id TEXT, content TEXT,
                sentiment_label TEXT, sentiment_score INTEGER,
                dimension_tags TEXT, entity_tags TEXT, keyword_tags TEXT,
                aggregation_keyword_tags TEXT,
                label_review_flag INTEGER NOT NULL DEFAULT 4
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

    def row(self, comment_id: str) -> Dict[str, Any]:
        cur = self._db.execute(f"SELECT * FROM `{TABLE}` WHERE comment_id = ?", (comment_id,))
        return dict(cur.fetchone())


def _row(comment_id, content, *, label="负向", score=-1, keywords=(),
         dims="[]", agg="[]", flag=4):
    return {
        "channel": "ctrip", "work_id": "w1", "scenic_id": "S1",
        "scenic_name": "示例景区", "comment_id": comment_id, "content": content,
        "sentiment_label": label, "sentiment_score": score,
        "dimension_tags": dims, "entity_tags": "[]",
        "keyword_tags": json.dumps(list(keywords), ensure_ascii=False),
        "aggregation_keyword_tags": agg, "label_review_flag": flag,
    }


@pytest.fixture()
def run(monkeypatch):
    """跑一次脚本，返回 (pool, fixer)。"""
    def _run(rows, *argv):
        pool = SqlitePool(rows)
        monkeypatch.setattr(fix, "MySQLPool", lambda _cfg: pool)
        cfg = load_config(require_db=False)
        args = fix.build_parser().parse_args(list(argv))
        fixer = fix.InquiryFixer(cfg, args)
        fixer.run()
        return pool, fixer
    return _run


# =========================================================== 你给的两个案例
def test_pure_inquiry_row_is_reset_to_neutral(run):
    pool, fixer = run([_row("c1", "最近人多吗", keywords=["人多"],
                            agg='["人多拥挤"]')])
    row = pool.row("c1")
    assert row["sentiment_label"] == "中性"
    assert row["sentiment_score"] == 0
    assert row["keyword_tags"] == "[]"
    assert row["aggregation_keyword_tags"] == "[]"      # 不再计入拥挤度
    assert fixer.pure_inquiry == 1


def test_age_and_inquiry_keywords_both_cleared(run):
    pool, _ = run([_row("c1", "人多吗？我们4岁多可去么", keywords=["人多", "4岁多"])])
    assert pool.row("c1")["keyword_tags"] == "[]"
    assert pool.row("c1")["sentiment_score"] == 0


# =========================================================== 不能改的行
def test_real_complaint_is_left_alone(run):
    """真抱怨一个字都不能动——这是这个脚本最危险的失败模式。"""
    pool, fixer = run([_row("c1", "人太多了，排队排了两小时",
                            keywords=["人太多", "排队排了两小时"],
                            agg='["人多拥挤","排队久"]')])
    row = pool.row("c1")
    assert json.loads(row["keyword_tags"]) == ["人太多", "排队排了两小时"]
    assert row["sentiment_score"] == -1
    assert fixer.written == 0                           # 没有变化就不写库


def test_half_question_keeps_the_review_half(run):
    """「风景很美，请问几点关门？」——评价留住，只摘疑问句里的词。"""
    pool, fixer = run([_row("c1", "风景很美，请问几点关门？", label="正向", score=1,
                            keywords=["风景很美", "几点关门"])])
    row = pool.row("c1")
    assert json.loads(row["keyword_tags"]) == ["风景很美"]
    assert row["sentiment_score"] == 1                   # 情感不动
    assert json.loads(row["aggregation_keyword_tags"]) == ["风景优美"]
    assert fixer.partial == 1


def test_human_reviewed_rows_are_never_touched(run):
    """label_review_flag 1/2/3 是人看过的结论，AI 不许覆盖。"""
    rows = [_row("c1", "最近人多吗", keywords=["人多"], flag=f) for f in (1, 2, 3)]
    for idx, row in enumerate(rows):
        row["comment_id"] = f"c{idx + 1}"
    pool, fixer = run(rows)
    assert fixer.scanned == 0
    for cid in ("c1", "c2", "c3"):
        assert json.loads(pool.row(cid)["keyword_tags"]) == ["人多"]


def test_include_reviewed_opens_the_door_explicitly(run):
    rows = [_row("c1", "最近人多吗", keywords=["人多"], flag=1)]
    pool, fixer = run(rows, "--include-reviewed")
    assert fixer.scanned == 1
    assert pool.row("c1")["keyword_tags"] == "[]"


# =========================================================== 运行特性
def test_dry_run_writes_nothing(run):
    pool, fixer = run([_row("c1", "最近人多吗", keywords=["人多"])], "--dry-run")
    assert json.loads(pool.row("c1")["keyword_tags"]) == ["人多"]
    assert fixer.written == 0
    assert fixer.pure_inquiry == 1                       # 但统计照常出


def test_fixed_rows_drop_out_of_the_next_run(run, monkeypatch):
    """改完的行第二遍不该再被扫到——否则没法放心重复执行。

    取数条件是 ``keyword_tags <> '[]'``，第一遍把它清成 [] 之后，
    第二遍的 count() 就看不见它了。这正是"修完即出队"的收敛条件。
    """
    pool, _ = run([_row("c1", "最近人多吗", keywords=["人多"])])
    assert pool.row("c1")["keyword_tags"] == "[]"

    monkeypatch.setattr(fix, "MySQLPool", lambda _cfg: pool)
    again = fix.InquiryFixer(load_config(require_db=False),
                             fix.build_parser().parse_args([]))
    assert again.count() == 0


def test_paging_covers_everything(run):
    rows = [_row(f"c{i:03d}", "最近人多吗", keywords=["人多"]) for i in range(25)]
    pool, fixer = run(rows, "--batch-size", "4")
    assert fixer.scanned == 25
    assert fixer.pure_inquiry == 25


def test_dirty_keyword_json_does_not_break_the_batch(run):
    rows = [_row("c1", "最近人多吗", keywords=["人多"])]
    rows.append(_row("c2", "还行", keywords=["还行"]))
    rows[1]["keyword_tags"] = "不是 JSON"
    pool, fixer = run(rows)
    assert fixer.scanned == 2
    assert pool.row("c1")["keyword_tags"] == "[]"


# =========================================================== 纯函数
def test_clean_keywords_is_the_rule_in_one_place():
    kept, dropped = fix.clean_keywords(["人多", "4岁多"], "人多吗？我们4岁多可去么")
    assert kept == [] and set(dropped) == {"人多", "4岁多"}

    kept, dropped = fix.clean_keywords(["人太多", "排队两小时"], "人太多了，排队两小时")
    assert kept == ["人太多", "排队两小时"] and dropped == []


def test_parse_list_survives_garbage():
    assert fix.parse_list('["人多"]') == ["人多"]
    assert fix.parse_list(None) == []
    assert fix.parse_list("不是 JSON") == []
    assert fix.parse_list('{"a":1}') == []
