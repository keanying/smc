"""标注结果写回。

只做一件事：按业务主键把五个标注字段 UPDATE 回 ``src_opinion_social_work_comment_di``。

三条设计约束：
    1. **只 UPDATE，不 INSERT。** 行是采集侧写进去的，标注侧无权新增，
       否则一旦主键口径不一致就会产生重复评论。
    2. **幂等。** 同一条评论重复标注只是覆盖同样的值，所以队列的
       at-least-once 语义不会造成脏数据。
    3. **表名列名在配置加载时已做标识符校验**（见 ``StorageConfig.validate``），
       所以这里可以安全地用 f-string 拼 SQL 骨架，值一律走参数绑定。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

import pymysql

from ..config import StorageConfig
from ..domain.models import CommentRecord, LabelResult
from ..logging_conf import get_logger
from .mysql import MySQLPool

__all__ = ["LabelRepository", "WriteReport"]

logger = get_logger(__name__)


@dataclass
class WriteReport:
    """一次批量写回的结果。"""

    attempted: int = 0
    matched: int = 0
    missed: int = 0          # 按主键没找到对应行
    elapsed_ms: int = 0

    def __str__(self) -> str:
        return (f"写回 {self.attempted} 条，命中 {self.matched} 条，"
                f"未命中 {self.missed} 条，耗时 {self.elapsed_ms}ms")


class LabelRepository:
    """标注结果仓储。线程安全（每次操作从池里借连接）。"""

    def __init__(self, pool: MySQLPool, cfg: StorageConfig) -> None:
        self._pool = pool
        self._cfg = cfg
        self._sql, self._value_order = self._build_update_sql(cfg)
        self._flag_sql = self._build_flag_only_sql(cfg)
        self._diagnosed = 0
        # 重复写回检测：同一进程里同一个主键被写第二次，说明这条评论被标了两遍
        # （白花一次模型调用）。用集合记住写过的键，超过上限就停止检测而不是吃内存。
        self._written_keys: set = set()
        self._written_keys_capped = False
        self.duplicate_writes = 0
        self._dup_lock = threading.Lock()

    # ------------------------------------------------------------------ SQL
    @staticmethod
    def _build_update_sql(cfg: StorageConfig) -> Tuple[str, List[str]]:
        """预编译 UPDATE 语句骨架。

        Returns:
            (SQL 文本, 参数顺序)。参数顺序里 SET 段用逻辑名，WHERE 段用 ``key:列名``。
        """
        set_parts: List[str] = []
        order: List[str] = []
        for logical, column in cfg.columns.items():
            set_parts.append(f"`{column}` = %s")
            order.append(logical)

        if cfg.review_column:
            set_parts.append(f"`{cfg.review_column}` = %s")
            order.append("review_flag")

        where_parts = []
        for column in cfg.key_columns:
            where_parts.append(f"`{column}` = %s")
            order.append(f"key:{column}")

        sql = (
            f"UPDATE `{cfg.table}` SET " + ", ".join(set_parts) +
            " WHERE " + " AND ".join(where_parts)
        )
        return sql, order

    @staticmethod
    def _build_flag_only_sql(cfg: StorageConfig) -> str:
        """只更新复核标记列的语句（标注失败时用）。

        标注失败不该往五个标注字段里写任何东西——写个假中性会把行占掉。
        但把标记置成 ai_failed 是有价值的：失败的行在 SQL 里能盘点、能筛出来重标，
        与 Redis 失败池互为补充。
        """
        if not cfg.review_column:
            return ""
        where = " AND ".join(f"`{c}` = %s" for c in cfg.key_columns)
        # ⚠️ 只标**还没有标签**的行。同一条评论在队列里有两份时（重复采集再推一次、
        #    补标和边采边标撞上、重启前留下的重试副本），成功的那份攒批写回了标签和 4，
        #    另一份晚些失败、立刻写 5，就把成功盖成了「AI 标注错误」——标签明明在。
        #    有标签就说明它已经标好了（或者人工复核过），失败不该改它的状态。
        label_col = cfg.columns.get("sentiment_label")
        if label_col:
            where += f" AND (`{label_col}` IS NULL OR `{label_col}` = '')"
        return f"UPDATE `{cfg.table}` SET `{cfg.review_column}` = %s WHERE {where}"

    def _params(self, record: CommentRecord, result: LabelResult,
                *, review_flag: int = 0) -> Tuple:
        """按 ``_value_order`` 拼出一行的绑定参数。"""
        row = result.to_row(ensure_ascii=self._cfg.json_ensure_ascii)
        row["review_flag"] = review_flag
        values = []
        for token in self._value_order:
            if token.startswith("key:"):
                column = token[4:]
                values.append(getattr(record, column, ""))
            else:
                values.append(row[token])
        return tuple(values)

    # ------------------------------------------------------------------ 写入
    def update_one(self, record: CommentRecord, result: LabelResult,
                   *, review_flag: int = 0) -> bool:
        """写回一条。返回是否命中了行。"""
        report = self.update_many([(record, result)], review_flags=[review_flag])
        return report.matched > 0

    def update_many(
        self,
        pairs: Sequence[Tuple[CommentRecord, LabelResult]],
        *,
        review_flags: Sequence[int] | None = None,
    ) -> WriteReport:
        """批量写回。

        Args:
            pairs: (原始评论, 标注结果) 列表。
            review_flags: 与 pairs 等长的人工复核标记，未配置复核列时忽略。

        Returns:
            :class:`WriteReport`。

        Raises:
            pymysql.MySQLError: 重试耗尽后仍失败，交由 worker 走 nack 重投。
        """
        if not pairs:
            return WriteReport()

        flags = list(review_flags or [0] * len(pairs))
        params = [self._params(rec, res, review_flag=flags[i])
                  for i, (rec, res) in enumerate(pairs)]

        started = time.monotonic()
        matched = self._executemany_with_retry(params)
        elapsed = int((time.monotonic() - started) * 1000)

        report = WriteReport(
            attempted=len(pairs),
            matched=matched,
            missed=max(0, len(pairs) - matched),
            elapsed_ms=elapsed,
        )
        self._check_duplicates(pairs)

        if report.missed:
            self._report_miss(report, pairs)
        else:
            logger.info("%s | %s", report, self._format_ids(pairs))
        return report

    #: 重复写回检测最多记多少个键，防止长跑把内存吃光
    _MAX_TRACKED_KEYS = 500_000

    def _row_key(self, record: CommentRecord) -> tuple:
        return tuple(str(getattr(record, c, "") or "") for c in self._cfg.key_columns)

    def _format_ids(self, pairs: Sequence[Tuple[CommentRecord, LabelResult]]) -> str:
        """把这一批的 comment_id 拼进日志。

        批量很大时只列前几个——真要逐条追踪应该看 DEBUG 级别的单条日志，
        而不是让 INFO 刷出几百个 ID。
        """
        ids = [rec.comment_id for rec, _ in pairs]
        if len(ids) <= 8:
            return "comment_id=" + ",".join(ids)
        return f"comment_id={','.join(ids[:8])}…(共 {len(ids)} 条)"

    def _check_duplicates(self, pairs: Sequence[Tuple[CommentRecord, LabelResult]]) -> None:
        """同一进程内同一主键被写第二次就告警。

        这是回答"是不是在重复标注"最直接的证据：写回幂等，重复写不会弄脏数据，
        但每一次重复都意味着白花了一次模型调用，值得知道。
        """
        with self._dup_lock:
            if self._written_keys_capped:
                return
            for record, _ in pairs:
                key = self._row_key(record)
                if key in self._written_keys:
                    self.duplicate_writes += 1
                    if self.duplicate_writes <= 10:
                        logger.warning(
                            "重复写回：comment_id=%s 在本进程内已经标注过一次了"
                            "（写回幂等不会弄脏数据，但白花了一次模型调用）",
                            record.comment_id)
                    elif self.duplicate_writes == 11:
                        logger.warning("重复写回已超过 10 次，后续不再逐条告警，"
                                       "最终数量见跑批报告")
                    continue
                self._written_keys.add(key)

            if len(self._written_keys) > self._MAX_TRACKED_KEYS:
                self._written_keys_capped = True
                self._written_keys.clear()
                logger.info("已写回超过 %d 条，停止重复写回检测以免占用内存",
                            self._MAX_TRACKED_KEYS)

    #: 每个进程最多做几次未命中诊断——诊断要多跑一条 SELECT，不能每次都做
    _MAX_DIAGNOSES = 3

    def _report_miss(self, report: WriteReport,
                     pairs: Sequence[Tuple[CommentRecord, LabelResult]]) -> None:
        """未命中时不止喊一声，还要说清楚到底是哪种未命中。

        连上 CLIENT.FOUND_ROWS 之后，"匹配 0 行"就只剩一个含义：**按主键没查到这一行**。
        但"按主键查不到"本身还有好几种可能，光看日志分不出来：
            - 主键列配错了（storage.key_columns）
            - 值带了前后空格（NO PAD 排序规则下空格是有意义的）
            - 大小写不一致（渠道名 Ctrip / ctrip）
            - 这一行确实被删了
        所以头几次未命中会回查一次，把真实原因写进日志。
        """
        logger.warning("%s | %s", report, self._format_ids(pairs))

        if self._diagnosed >= self._MAX_DIAGNOSES or not pairs:
            return
        self._diagnosed += 1
        try:
            hint = self.diagnose_miss(pairs[0][0])
        except Exception as exc:                    # noqa: BLE001
            logger.warning("未命中诊断失败：%s", exc)
            return
        logger.warning("未命中诊断：%s", hint)

    def diagnose_miss(self, record: CommentRecord) -> str:
        """回查一条未命中的记录，判断到底卡在哪一列。

        逐列放宽条件重查：全主键查不到就单查 comment_id，
        能查到就说明是另外那几列对不上，并把库里的真实值打出来对比。
        """
        keys = list(self._cfg.key_columns)
        cols = ", ".join(f"`{c}`" for c in keys)

        # 1) 全主键精确匹配
        where = " AND ".join(f"`{c}` = %s" for c in keys)
        params = [getattr(record, c, "") for c in keys]
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {cols} FROM `{self._cfg.table}` WHERE {where} LIMIT 1",
                    tuple(params))
                if cur.fetchone():
                    return (f"主键能查到这一行，说明写回时它确实被匹配了。"
                            f"若仍报未命中，检查是不是并发把同一条评论标了两次")

                # 2) 只按 comment_id 查，看看是"行不在"还是"其它主键列对不上"
                cur.execute(
                    f"SELECT {cols} FROM `{self._cfg.table}` "
                    f"WHERE `comment_id` = %s LIMIT 3",
                    (record.comment_id,))
                rows = cur.fetchall()

        if not rows:
            return (f"comment_id={record.comment_id!r} 在表里根本不存在——"
                    f"要么这行被删了，要么取数和写回连的不是同一个库")

        mismatches = []
        for row in rows:
            for col in keys:
                mine = str(getattr(record, col, ""))
                theirs = str(row.get(col, ""))
                if mine != theirs:
                    mismatches.append(f"{col}: 引擎={mine!r} 库里={theirs!r}")
        detail = "；".join(dict.fromkeys(mismatches)) or "所有主键列看起来一致"
        return (f"comment_id={record.comment_id!r} 存在但整组主键匹配不上 → {detail}。"
                f"注意前后空格与大小写：utf8mb4_0900_ai_ci 是 NO PAD 排序规则，"
                f"尾部空格参与比较")

    def _executemany_with_retry(self, params: List[Tuple], *, sql: str = "") -> int:
        """带重试的批量执行，返回受影响行数。

        只对「连接类」错误重试；语法/字段类错误立即抛出，重试无意义。
        """
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        affected = cur.executemany(sql or self._sql, params)
                    conn.commit()
                    # executemany 返回值在部分驱动版本下为 None，回退用 rowcount
                    return int(affected if affected is not None else len(params))
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
                last_exc = exc
                wait = 0.5 * attempt
                logger.warning("写库第 %d 次失败（%s），%.1fs 后重试", attempt, exc, wait)
                time.sleep(wait)
            except pymysql.MySQLError:
                raise
        raise last_exc  # type: ignore[misc]

    def mark_failed(self, records: Sequence[CommentRecord]) -> WriteReport:
        """把标注失败的行标记成 ``ai_failed``，**不碰五个标注字段**。

        未配置 ``storage.review_column`` 时直接返回空报告——没有那一列就没法标记，
        这不是错误，失败记录仍然在 Redis 失败池里。

        Args:
            records: 标注失败的评论。
        """
        if not records or not self._flag_sql:
            return WriteReport()

        flag = int(self._cfg.review_flags.get("ai_failed", 5))
        params = [tuple([flag] + [getattr(r, c, "") for c in self._cfg.key_columns])
                  for r in records]

        started = time.monotonic()
        matched = self._executemany_with_retry(params, sql=self._flag_sql)
        report = WriteReport(
            attempted=len(records), matched=matched,
            missed=max(0, len(records) - matched),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info("标记标注失败 %d 条（%s=%d）；已有标签的 %d 条不改",
                    report.matched, self._cfg.review_column, flag, report.missed)
        return report

    # ------------------------------------------------------------------ 自检
    def ensure_schema(self) -> None:
        """启动期校验：表存在，且配置里引用的每一列都存在。

        Raises:
            RuntimeError: 表或列缺失。让它在启动时炸，而不是跑到第一批写回才炸。
        """
        wanted = set(self._cfg.key_columns) | set(self._cfg.columns.values())
        if self._cfg.review_column:
            wanted.add(self._cfg.review_column)

        sql = (
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
        )
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (self._cfg.table,))
                existing = {row["COLUMN_NAME"] for row in cur.fetchall()}

        if not existing:
            raise RuntimeError(f"表 {self._cfg.table} 不存在（或当前账号无权限）")
        missing = sorted(wanted - existing)
        if missing:
            raise RuntimeError(
                f"表 {self._cfg.table} 缺少配置中引用的列：{missing}；"
                f"请修正 config.yaml 的 storage 段"
            )
        logger.info("表结构自检通过：%s（%d 列）", self._cfg.table, len(existing))

    def fetch_unlabeled(self, limit: int = 100,
                        extra_where: str = "") -> List[Dict]:
        """补跑用：捞出尚未标注的行。

        正常链路是采集侧实时推数据进队列，本方法只服务于
        「补历史」「跑回溯」这类一次性任务，不参与主流程。
        """
        label_col = self._cfg.columns["sentiment_label"]
        select_cols = ", ".join(f"`{c}`" for c in [
            "scenic_id", "scenic_name", "channel", "work_id", "comment_id",
            "commenter_id", "content", "likes", "extra_content", "publish_time",
        ])
        where = f"(`{label_col}` IS NULL OR `{label_col}` = '')"
        if extra_where:
            where += f" AND ({extra_where})"
        sql = f"SELECT {select_cols} FROM `{self._cfg.table}` WHERE {where} LIMIT %s"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(limit),))
                return list(cur.fetchall())
