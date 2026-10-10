"""有标签的评论不能被标成「AI 标注错误」。

线上：AI 标注错误 1548 条，未标注只有 546——上千条明明标好了（有标签），
复核状态却停在 5。同一条评论在队列里有两份时，成功的那份攒批写回标签和 4，
晚到的失败那份立刻写 5，把成功盖掉了。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pymysql
import pytest

from app.labeling import config_bridge
from app.labeling.manager import _fix_labeled_but_failed

config_bridge.ensure_on_path()
from opinion_labeling_engine.config import MySQLConfig, StorageConfig   # noqa: E402
from opinion_labeling_engine.domain.models import CommentRecord          # noqa: E402
from opinion_labeling_engine.storage.mysql import MySQLPool             # noqa: E402
from opinion_labeling_engine.storage.repository import LabelRepository  # noqa: E402

DB = "smc_failed_flag"


@pytest.fixture()
def db():
    try:
        admin = pymysql.connect(host=os.environ.get("SMC_MYSQL_HOST", "127.0.0.1"),
                                user=os.environ.get("SMC_MYSQL_USER", "root"),
                                password=os.environ.get("SMC_MYSQL_PASSWORD", ""),
                                autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"没有 MySQL：{exc}")
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
        cur.execute(f"DROP TABLE IF EXISTS {DB}.t")
        cur.execute(f"CREATE TABLE {DB}.t (channel VARCHAR(20), scenic_id VARCHAR(20), "
                    "comment_id VARCHAR(20), sentiment_label VARCHAR(20), "
                    "sentiment_score INT, dimension_tags TEXT, entity_tags TEXT, "
                    "keyword_tags TEXT, label_review_flag TINYINT NULL)")
    pool = MySQLPool(MySQLConfig(
        host=os.environ.get("SMC_MYSQL_HOST", "127.0.0.1"),
        user=os.environ.get("SMC_MYSQL_USER", "root"),
        password=os.environ.get("SMC_MYSQL_PASSWORD", ""), database=DB, pool_size=2))
    cfg = StorageConfig(table="t", review_column="label_review_flag", columns={
        "sentiment_label": "sentiment_label", "sentiment_score": "sentiment_score",
        "dimension_tags": "dimension_tags", "entity_tags": "entity_tags",
        "keyword_tags": "keyword_tags"})

    def flags():
        with admin.cursor() as cur:
            cur.execute(f"SELECT comment_id, label_review_flag FROM {DB}.t ORDER BY comment_id")
            return dict(cur.fetchall())

    def run(sql, *args):
        with admin.cursor() as cur:
            cur.execute(sql.replace("{DB}", DB), args)

    yield SimpleNamespace(pool=pool, cfg=cfg, flags=flags, run=run)
    pool.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {DB}")
    admin.close()


def _rec(cid):
    return CommentRecord.from_dict({"channel": "douyin", "scenic_id": "S1", "comment_id": cid,
                                    "content": "x"})


def test_late_failure_does_not_overwrite_a_successful_label(db):
    db.run("INSERT INTO {DB}.t (channel, scenic_id, comment_id, sentiment_label, label_review_flag)"
           " VALUES ('douyin','S1','ok','正向',4), ('douyin','S1','human','负向',1),"
           " ('douyin','S1','empty',NULL,0), ('douyin','S1','blank','',0)")
    repo = LabelRepository(db.pool, db.cfg)
    # 成功的那份已经写回了（'ok'），另一份副本这时才失败
    repo.mark_failed([_rec("ok"), _rec("human"), _rec("empty"), _rec("blank")])
    assert db.flags() == {"ok": 4, "human": 1, "empty": 5, "blank": 5}, \
        "有标签的（AI 成功的、人工复核过的）不能被失败改成 5；没标签的照样标 5"


def test_startup_repair_turns_labeled_failures_into_success(db):
    db.run("INSERT INTO {DB}.t (channel, scenic_id, comment_id, sentiment_label, label_review_flag)"
           " VALUES ('douyin','S1','a','正向',5), ('douyin','S1','b','中性',5),"
           " ('douyin','S1','c',NULL,5), ('douyin','S1','d','',5), ('douyin','S1','e','负向',2)")
    engine = SimpleNamespace(cfg=SimpleNamespace(storage=db.cfg), _pool=db.pool)
    assert _fix_labeled_but_failed(engine) == 2
    assert db.flags() == {"a": 4, "b": 4, "c": 5, "d": 5, "e": 2}
    assert _fix_labeled_but_failed(engine) == 0, "幂等：每次启动跑一遍不会乱改"
