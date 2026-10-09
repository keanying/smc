"""表改名：老库里的数据必须原样带过来。

改表名这件事本身不难，难的是**升级路径**：
schema.sql 全是 `CREATE TABLE IF NOT EXISTS`，如果先建表再改名，
新的空表已经占了位置，`RENAME TABLE` 会直接报 1050，
老表就孤零零留在库里——界面上看就是"以前采的数据全没了"。
所以这里用真实 MySQL 验证顺序和结果。
"""
from __future__ import annotations

import os

import pytest

from app.db.tables import LEGACY_RENAMES, TASK, WORKS

TEST_DB = "scenic_media_rename"


@pytest.fixture()
def legacy_db():
    """造一个"老版本"的库：只有旧表名，里面有一行真数据。"""
    os.environ["SMC_MYSQL_DATABASE"] = TEST_DB
    os.environ["SMC_REDIS_ENABLED"] = "false"

    from app.core.config import load_config, reset_cache
    reset_cache()
    cfg = load_config(use_cache=False)
    mysql = cfg.mysql

    import pymysql
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]),
        user=mysql["user"], password=mysql["password"], autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` DEFAULT CHARSET utf8mb4")
        cur.execute(f"USE `{TEST_DB}`")
        # 老表结构不用完整，改名只认表名
        cur.execute(
            "CREATE TABLE `task` ("
            "  `id` BIGINT NOT NULL AUTO_INCREMENT,"
            "  `task_id` VARCHAR(64) NOT NULL,"
            "  `task_name` VARCHAR(200) NOT NULL,"
            "  PRIMARY KEY (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        cur.execute(
            "INSERT INTO `task` (task_id, task_name) VALUES ('old-001', '升级前建的任务')"
        )
    yield conn, cfg
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    conn.close()
    reset_cache()


@pytest.mark.asyncio
async def test_legacy_tables_are_renamed_with_their_data(legacy_db):
    conn, cfg = legacy_db
    from app.core.db import Database

    db = Database(cfg)
    await db.connect()          # connect 里会 migrate
    try:
        rows = await db.fetch_all(f"SELECT task_id, task_name FROM `{TASK}`")
    finally:
        await db.close()

    assert [r["task_id"] for r in rows] == ["old-001"], (
        "老表里的数据必须跟着改名一起过来，不能只建一张空的新表"
    )
    assert rows[0]["task_name"] == "升级前建的任务"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s", (TEST_DB,)
        )
        names = {row[0] for row in cur.fetchall()}
    assert "task" not in names, "改名后不该还留着老表"
    assert set(LEGACY_RENAMES.values()) <= names, "10 张表都得在"


@pytest.mark.asyncio
async def test_rename_is_idempotent(legacy_db):
    """重复启动不能出错，也不能把已有数据覆盖成空表。"""
    _, cfg = legacy_db
    from app.core.db import Database

    for _ in range(2):
        db = Database(cfg)
        await db.connect()
        await db.migrate()
        await db.close()

    db = Database(cfg)
    await db.connect()
    try:
        rows = await db.fetch_all(f"SELECT task_id FROM `{TASK}`")
        # 新表也确实建出来了
        count = await db.fetch_value(f"SELECT COUNT(*) AS c FROM `{WORKS}`", [], 0)
    finally:
        await db.close()
    assert [r["task_id"] for r in rows] == ["old-001"]
    assert int(count or 0) == 0


def test_every_table_name_carries_the_prefix():
    """表名是对下游数仓的契约，前缀漏一个就会被当成别的系统的表。"""
    from app.db import tables

    names = [v for k, v in vars(tables).items()
             if k.isupper() and isinstance(v, str)]
    assert names, "tables.py 里应该有表名常量"
    assert all(n.startswith("src_opinion_") for n in names), names

    #: 改造之后**新增**的表，历史上没有对应的旧名，自然不在 LEGACY_RENAMES 里。
    #: 这里显式登记而不是把断言放宽，是为了"漏写旧名映射"仍然会被抓出来——
    #: 放宽成 issubset 的话，把一张老表改了名却忘了登记旧名，测试照样绿，
    #: 而用户升级后那张老表的数据就全"消失"了。
    from app.db.tables import (
        ACCOUNT_QUOTA, ARCHIVE_REGION, QUNAR_REGION, SCENIC_FILTER_WORD,
        SCENIC_POI_INFO,
    )
    born_after_rename = {SCENIC_FILTER_WORD, SCENIC_POI_INFO, QUNAR_REGION,
                         ARCHIVE_REGION, ACCOUNT_QUOTA}

    assert set(LEGACY_RENAMES.values()) == set(names) - born_after_rename
    # 旧名映射不能指向一个不存在的表名常量
    assert set(LEGACY_RENAMES.values()) <= set(names)


@pytest.mark.asyncio
async def test_missing_indexes_are_added_to_existing_tables(legacy_db):
    """往 schema.sql 里加索引，老库是加不上的——必须单独补建。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不做**。
    用户升级完，表结构还是旧的、查询照样慢，而且没有任何提示。
    """
    _, cfg = legacy_db
    from app.core.db import Database
    from app.db.tables import EXTRA_INDEXES

    db = Database(cfg)
    await db.connect()
    try:
        # 先把索引删掉，模拟"老库没有这些索引"
        for table, name, _ in EXTRA_INDEXES:
            try:
                await db.execute(f"DROP INDEX `{name}` ON `{table}`")
            except Exception:  # noqa: BLE001
                pass
        rows = await db.fetch_all(
            "SELECT TABLE_NAME AS t, INDEX_NAME AS i FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        before = {(str(r["t"]), str(r["i"])) for r in rows}
        assert not any((t, n) in before for t, n, _ in EXTRA_INDEXES)

        # 再跑一次建表流程：应该把缺的补回来
        await db.migrate()

        rows = await db.fetch_all(
            "SELECT TABLE_NAME AS t, INDEX_NAME AS i FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        after = {(str(r["t"]), str(r["i"])) for r in rows}
        missing = [f"{t}.{n}" for t, n, _ in EXTRA_INDEXES if (t, n) not in after]
        assert not missing, f"这些索引没补上：{missing}"

        # 幂等：再跑一次不能报错
        await db.migrate()
    finally:
        await db.close()
