"""MySQL 连接池。

用一个极简的池而不是引入 SQLAlchemy/DBUtils：
这套引擎对数据库只有「按主键 UPDATE」一种操作，重依赖不划算。
池只保证两件事——连接复用、断线自愈（``ping(reconnect=True)``）。
"""

from __future__ import annotations

import queue as _stdqueue
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import pymysql
from pymysql.connections import Connection
from pymysql.constants import CLIENT

from ..config import MySQLConfig
from ..logging_conf import get_logger

__all__ = ["MySQLPool"]

logger = get_logger(__name__)


class MySQLPool:
    """定长连接池，线程安全。"""

    def __init__(self, cfg: MySQLConfig) -> None:
        self._cfg = cfg
        self._pool: _stdqueue.Queue = _stdqueue.Queue(maxsize=cfg.pool_size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> Connection:
        return pymysql.connect(
            host=self._cfg.host,
            port=self._cfg.port,
            user=self._cfg.user,
            password=self._cfg.password,
            database=self._cfg.database,
            charset=self._cfg.charset,
            connect_timeout=self._cfg.connect_timeout,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            # CLIENT.FOUND_ROWS：让 UPDATE 的 rowcount 表示「匹配到几行」而不是
            # 「改变了几行」。默认语义下，把同样的值再写一遍会返回 0，
            # 于是写回逻辑会误判成「主键没匹配上」并刷一堆假告警。
            # 重复标注同一条评论（表里有重复行、或队列重投）时这种情况很常见。
            client_flag=CLIENT.FOUND_ROWS,
        )

    def _acquire(self) -> Connection:
        try:
            conn = self._pool.get_nowait()
        except _stdqueue.Empty:
            with self._lock:
                if self._created < self._cfg.pool_size:
                    self._created += 1
                    return self._connect()
            # 池已满：阻塞等一个还回来
            conn = self._pool.get(block=True)

        try:
            conn.ping(reconnect=True)      # 自愈：连接被服务端超时踢掉时自动重连
        except Exception:                   # noqa: BLE001
            try:
                conn.close()
            except Exception:               # noqa: BLE001
                pass
            conn = self._connect()
        return conn

    def _release(self, conn: Optional[Connection], *, broken: bool = False) -> None:
        if conn is None:
            return
        if broken or self._closed:
            try:
                conn.close()
            finally:
                with self._lock:
                    self._created -= 1
            return
        try:
            self._pool.put_nowait(conn)
        except _stdqueue.Full:              # pragma: no cover
            conn.close()
            with self._lock:
                self._created -= 1

    # ------------------------------------------------------------------ 上下文
    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """借出一个连接，异常时自动回滚并标记连接为损坏。"""
        conn = self._acquire()
        broken = False
        try:
            yield conn
        except Exception:
            broken = True
            try:
                conn.rollback()
            except Exception:               # noqa: BLE001
                pass
            raise
        finally:
            self._release(conn, broken=broken)

    # ------------------------------------------------------------------ 自检
    def ping(self) -> bool:
        """启动自检：库连不上要在启动期暴露。"""
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return True
        except Exception as exc:            # noqa: BLE001
            logger.error("MySQL 连接失败：%s", exc)
            return False

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
            except _stdqueue.Empty:
                break
            try:
                conn.close()
            except Exception:               # noqa: BLE001
                pass
        with self._lock:
            self._created = 0
