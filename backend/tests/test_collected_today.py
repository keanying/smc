"""「今天已经采过」必须以**真的采到评论**为准。

作品行是先写的——评论翻不翻得到都会写。所以只看作品表的话，
一条作品因为采集器出问题一条评论都没拿到，照样被记成"今天采过"，
当天再怎么重跑都跳过它，评论**永远补不回来**。

实跑现场（2026-09-05 八大处公园）：前几轮因为点错卡片，
27 条作品的评论全是 0，之后每次重跑第一个关键字都是
「今天已经采过它的评论，本轮不再翻」——用户看到的是"第一个关键字直接没采"。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

TEST_DB = "scenic_media_collected_today"
SCENIC = "S-BADACHU"
CHANNEL = "kuaishou"


@pytest.fixture()
async def repo():
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"
    from app.core.config import load_config, reset_cache
    reset_cache()
    cfg = load_config(use_cache=False)

    import pymysql
    mysql = cfg.mysql
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]),
                           user=mysql["user"], password=mysql["password"],
                           autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` DEFAULT CHARSET utf8mb4")
    conn.close()

    from app.core.db import Database
    from app.repositories.data_repo import DataRepository
    db = Database(cfg)
    await db.connect()
    yield DataRepository(db), db
    await db.close()
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]),
                           user=mysql["user"], password=mysql["password"],
                           autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    conn.close()
    reset_cache()


async def _add_work(db, work_id: str, when: datetime):
    from app.db import tables
    await db.execute(
        f"INSERT INTO `{tables.WORKS}` "
        "(channel, work_id, scenic_id, scenic_name, crawl_time) "
        "VALUES (%s, %s, %s, %s, %s)",
        [CHANNEL, work_id, SCENIC, "八大处公园", when],
    )


async def _add_comment(db, work_id: str, comment_id: str, when: datetime):
    from app.db import tables
    await db.execute(
        f"INSERT INTO `{tables.COMMENTS}` "
        "(channel, comment_id, work_id, scenic_id, scenic_name, crawl_time) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [CHANNEL, comment_id, work_id, SCENIC, "八大处公园", when],
    )


@pytest.mark.asyncio
async def test_only_works_with_comments_count_as_done_today(repo):
    data, db = repo
    today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    midnight = today.replace(hour=0, minute=0)

    await _add_work(db, "W_HAS", today)          # 今天采到了评论
    await _add_comment(db, "W_HAS", "C1", today)
    await _add_work(db, "W_EMPTY", today)        # 今天只写了作品行，评论 0 条
    await _add_work(db, "W_OLD", today - timedelta(days=3))   # 前几天的
    await _add_comment(db, "W_OLD", "C_OLD", today - timedelta(days=3))

    done = await data.work_ids_collected_since(SCENIC, CHANNEL, midnight)

    assert "W_HAS" in done, "今天真的采到评论的，应该跳过"
    assert "W_EMPTY" not in done, (
        "评论 0 条的被算成「今天已采过」了——"
        "采集器出问题那几轮的空结果会把这条作品当天永久锁死，评论再也补不回来"
    )
    assert "W_OLD" not in done, "前几天的不该算今天"


@pytest.mark.asyncio
async def test_comment_written_today_for_an_older_work_still_counts(repo):
    """作品是前几天入的库，但**评论是今天补的**——那也算今天采过了。"""
    data, db = repo
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    await _add_work(db, "W_MIX", today)
    await _add_comment(db, "W_MIX", "C_NEW", today)
    done = await data.work_ids_collected_since(
        SCENIC, CHANNEL, today.replace(hour=0, minute=0))
    assert "W_MIX" in done
