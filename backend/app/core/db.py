"""MySQL 访问层：aiomysql 连接池 + 启动时自动建表。

只提供最小够用的封装（fetch_all / fetch_one / execute / execute_many / 事务），
不引入 ORM——采集写入是高频批量 upsert，手写 SQL 更可控。
"""
from __future__ import annotations

import asyncio
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence

import aiomysql

from .config import Config
from .logging import get_logger

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


class Database:
    """全局单例式连接池封装。"""

    def __init__(self, config: Config):
        self._config = config
        self._pool: Optional[aiomysql.Pool] = None
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------
    async def connect(self) -> None:
        if self._pool is not None:
            return
        async with self._lock:
            if self._pool is not None:
                return
            cfg = self._config.get("mysql", {})
            await self._ensure_database_exists(cfg)
            self._pool = await aiomysql.create_pool(
                host=cfg["host"],
                port=int(cfg["port"]),
                user=cfg["user"],
                password=cfg["password"],
                db=cfg["database"],
                charset=cfg.get("charset", "utf8mb4"),
                minsize=int(cfg.get("pool_min", 2)),
                maxsize=int(cfg.get("pool_max", 20)),
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                # 采集入库经常一次插几百条，放宽单包上限由服务端决定
                connect_timeout=10,
                # ⚠️ 不设的话 aiomysql 默认永不回收连接（-1），
                # 而 MySQL 的 wait_timeout 默认 8 小时会把空闲连接掐掉——
                # 夜里没人用，第二天早上第一个请求就报
                # "MySQL server has gone away"。一小时回收一次，稳。
                pool_recycle=int(cfg.get("pool_recycle_seconds", 3600)),
            )
            logger.info(
                "MySQL 连接池已建立：%s:%s/%s（%s~%s 连接）",
                cfg["host"], cfg["port"], cfg["database"],
                cfg.get("pool_min", 2), cfg.get("pool_max", 20),
            )
            if cfg.get("auto_migrate", True):
                await self.migrate()

    async def _ensure_database_exists(self, cfg: Dict[str, Any]) -> None:
        """库不存在时自动创建，省掉一步人工操作。"""
        conn = None
        try:
            conn = await aiomysql.connect(
                host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
                password=cfg["password"], charset=cfg.get("charset", "utf8mb4"),
                autocommit=True, connect_timeout=10,
            )
            async with conn.cursor() as cur:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=r".*database exists.*")
                    await cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                        f"DEFAULT CHARACTER SET {cfg.get('charset', 'utf8mb4')}"
                    )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"无法连接 MySQL（{cfg['host']}:{cfg['port']}）：{exc}。"
                f"请检查 config.yaml 的 mysql 段，以及数据库是否已启动。"
            ) from exc
        finally:
            if conn is not None:
                conn.close()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            logger.info("MySQL 连接池已关闭")

    @property
    def pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("数据库尚未初始化，请先 await db.connect()")
        return self._pool

    # ---------------- 建表 ----------------
    async def migrate(self, schema_path: Path | None = None) -> None:
        path = schema_path or SCHEMA_PATH
        if not path.exists():
            raise FileNotFoundError(f"未找到建表脚本：{path}")
        await self._rename_legacy_tables()
        sql_text = path.read_text(encoding="utf-8")
        statements = _split_sql(sql_text)
        async with self.acquire() as cur:
            # CREATE TABLE IF NOT EXISTS 在表已存在时会发 1050 警告，
            # 这是幂等建表的正常结果，不该刷屏。
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=r".*already exists.*")
                for statement in statements:
                    await cur.execute(statement)
        logger.info("数据库结构已就绪（执行 %d 条 DDL）", len(statements))
        await self._ensure_extra_columns()
        await self._ensure_extra_indexes()

    async def _ensure_extra_columns(self) -> None:
        """给已存在的表补上后加的列。

        ⚠️ 和索引同理：`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，
        往 schema.sql 里加一列，老库是加不上的——之后 INSERT/SELECT 引用
        新列直接报 Unknown column。这里按 (表, 列) 比对，缺哪个补哪个。
        """
        from ..db.tables import EXTRA_COLUMNS
        async with self.acquire() as cur:
            await cur.execute(
                "SELECT TABLE_NAME AS t, COLUMN_NAME AS c FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
            existing = {(str(r["t"]), str(r["c"])) for r in await cur.fetchall()}
            added = []
            for table, column, definition in EXTRA_COLUMNS:
                if (table, column) in existing:
                    continue
                try:
                    await cur.execute(
                        f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("补建列 %s.%s 失败：%s", table, column, exc)
                    continue
                added.append(f"{table}.{column}")
            if added:
                logger.info("已补建列：%s", "、".join(added))

    async def _ensure_extra_indexes(self) -> None:
        """给已存在的表补上后加的索引。

        ⚠️ `CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不做**，
        所以往 schema.sql 里加一个 KEY，老库是加不上的——
        用户升级完表结构还是旧的，页面照样慢，而且没有任何提示。
        这里按索引名比对，缺哪个补哪个，建过了就跳过。
        """
        from ..db.tables import EXTRA_INDEXES

        async with self.acquire() as cur:
            await cur.execute(
                "SELECT TABLE_NAME AS t, INDEX_NAME AS i FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
            existing = {(str(r["t"]), str(r["i"])) for r in await cur.fetchall()}

            added = []
            for table, name, definition in EXTRA_INDEXES:
                if (table, name) in existing:
                    continue
                try:
                    await cur.execute(
                        f"CREATE INDEX `{name}` ON `{table}` {definition}"
                    )
                except Exception as exc:  # noqa: BLE001
                    # 建索引失败不能挡住服务启动：大表上建索引可能超时，
                    # 但没有索引只是慢，服务本身还是能用的。
                    logger.warning("补建索引 %s.%s 失败：%s", table, name, exc)
                    continue
                added.append(f"{table}.{name}")
            if added:
                logger.info("已补建索引：%s（大表上这一步可能要等一会）", "、".join(added))

    async def _rename_legacy_tables(self) -> None:
        """把早期的短表名改成正式表名（task -> src_opinion_crawl_task 等）。

        必须在建表**之前**跑：schema.sql 里全是 CREATE TABLE IF NOT EXISTS，
        先建表的话新表已经存在，老表就只能孤零零留在库里，
        用户上一轮采到的数据从界面上直接消失——这种丢法最难发现。

        只在「老表在、新表不在」时动手，所以重复启动是幂等的。
        """
        from ..db.tables import LEGACY_RENAMES

        existing = set()
        async with self.acquire() as cur:
            await cur.execute(
                "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
            for row in await cur.fetchall():
                existing.add(str(row["t"]))

            renamed = []
            for legacy, current in LEGACY_RENAMES.items():
                if legacy in existing and current not in existing:
                    await cur.execute(f"RENAME TABLE `{legacy}` TO `{current}`")
                    renamed.append(f"{legacy} -> {current}")
            if renamed:
                logger.warning("已把历史表改名（数据原样保留）：%s", "、".join(renamed))

    # ---------------- 查询 ----------------
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiomysql.DictCursor]:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                yield cur

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiomysql.DictCursor]:
        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    yield cur
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def fetch_all(self, sql: str, args: Sequence[Any] | None = None) -> List[Dict[str, Any]]:
        async with self.acquire() as cur:
            await cur.execute(sql, args or ())
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, args: Sequence[Any] | None = None) -> Optional[Dict[str, Any]]:
        async with self.acquire() as cur:
            await cur.execute(sql, args or ())
            return await cur.fetchone()

    async def fetch_value(self, sql: str, args: Sequence[Any] | None = None, default: Any = None) -> Any:
        row = await self.fetch_one(sql, args)
        if not row:
            return default
        return next(iter(row.values()), default)

    async def execute(self, sql: str, args: Sequence[Any] | None = None) -> int:
        """返回受影响行数。"""
        async with self.acquire() as cur:
            await cur.execute(sql, args or ())
            return cur.rowcount

    async def execute_returning_id(self, sql: str, args: Sequence[Any] | None = None) -> int:
        async with self.acquire() as cur:
            await cur.execute(sql, args or ())
            return cur.lastrowid

    async def execute_many(self, sql: str, args_list: Iterable[Sequence[Any]]) -> int:
        rows = list(args_list)
        if not rows:
            return 0
        async with self.acquire() as cur:
            await cur.executemany(sql, rows)
            return cur.rowcount

    async def ping(self) -> bool:
        try:
            await self.fetch_value("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False


def _split_sql(text: str) -> List[str]:
    """按分号切分 DDL，忽略 -- 注释行与空语句。"""
    lines: List[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(raw_line)
    joined = "\n".join(lines)
    return [s.strip() for s in joined.split(";") if s.strip()]


_db: Optional[Database] = None


def init_db(config: Config) -> Database:
    global _db
    _db = Database(config)
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db(config)")
    return _db
