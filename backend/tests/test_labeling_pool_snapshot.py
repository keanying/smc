"""标注引擎连接池：连接还回池子时要结束事务，否则下一个借到的人读的是旧快照。

现象：补标刚把一批评论标完（别的连接 commit 了），补标自己再去数/捞「未标注」时
借到同一个连接，看到的还是开工前那份快照——刚标完的又被当成未标注重新提交，
重复标、重复花钱；服务启动时数过一次 0 条，之后新进来的评论也永远看不到。
"""
from __future__ import annotations

import os

import pymysql
import pytest

from app.labeling import config_bridge

config_bridge.ensure_on_path()
from opinion_labeling_engine.config import MySQLConfig            # noqa: E402
from opinion_labeling_engine.storage.mysql import MySQLPool       # noqa: E402

DB = "smc_pool_snapshot"


def _admin():
    try:
        return pymysql.connect(host=os.environ.get("SMC_MYSQL_HOST", "127.0.0.1"),
                               user=os.environ.get("SMC_MYSQL_USER", "root"),
                               password=os.environ.get("SMC_MYSQL_PASSWORD", ""),
                               autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"没有 MySQL：{exc}")


def test_reused_connection_sees_rows_committed_elsewhere():
    admin = _admin()
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
        cur.execute(f"CREATE TABLE IF NOT EXISTS {DB}.t (id INT) ENGINE=InnoDB")
        cur.execute(f"DELETE FROM {DB}.t")
    pool = MySQLPool(MySQLConfig(
        host=os.environ.get("SMC_MYSQL_HOST", "127.0.0.1"),
        user=os.environ.get("SMC_MYSQL_USER", "root"),
        password=os.environ.get("SMC_MYSQL_PASSWORD", ""),
        database=DB, pool_size=1))           # 只有一个连接：第二次一定借到同一个

    def count() -> int:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM t")
                row = cur.fetchone()
                return int(row["c"] if isinstance(row, dict) else row[0])

    try:
        assert count() == 0
        with admin.cursor() as cur:          # 另一个连接写入并提交（相当于 worker 标完写回）
            cur.execute(f"INSERT INTO {DB}.t VALUES (1), (2)")
        assert count() == 2, "复用的连接还在读旧快照"
    finally:
        pool.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE {DB}")
        admin.close()
