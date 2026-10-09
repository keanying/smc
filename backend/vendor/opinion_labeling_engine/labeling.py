#!/usr/bin/env python3
"""从 src_opinion_social_work_comment_di 读评论 → 喂进标注引擎 → 写回同一张表。

    SELECT channel, work_id, scenic_id, scenic_name, comment_id, content
    FROM   src_opinion_social_work_comment_di
    ...
    engine.submit(row)

用法
----
    # 只标未标注的（默认），跑到没有为止。
    # **这条命令就是"继续标注"**：已标注的行不匹配取数条件，重跑一次自然接着往下标，
    # 中断了、机器重启了、跑了一半停了，都是同一条命令续上。
    python scripts/label_from_table.py

    # 只标某个景区、某个渠道
    python scripts/label_from_table.py --scenic-id PFTSCA01009835 --channel ctrip

    # 只标最近 7 天的，最多 2000 条
    python scripts/label_from_table.py --since 2026-08-30 --limit 2000

    # 干跑：不写库，结果打日志 + 存 CSV，先看看标得对不对
    python scripts/label_from_table.py --limit 50 --dry-run --out check.csv

    # 全量重标（含已标注的行）—— 需要一个唯一且可排序的列做游标
    python scripts/label_from_table.py --mode all --order-by comment_id

两种取数模式
------------
``pending``（默认）
    每轮 ``SELECT ... WHERE sentiment_label IS NULL OR '' LIMIT batch``，
    处理完写回后这批行就不再匹配条件，下一轮自然取到新的一批 —— 像抽水一样把表抽干。
    不需要任何排序列，也不怕中途新增数据；断点续跑天然支持（重跑一次接着抽）。
    代价是每批之间要等写回完成，批与批之间有一个短暂的空档。

``all``
    重标所有行（包括已标注的）。这时过滤条件不会随处理而收缩，
    必须用游标翻页：``WHERE <order_by> > <上一批最大值> ORDER BY <order_by> LIMIT batch``。
    ``--order-by`` 指定的列**必须唯一且可排序**，否则会漏行或重复。

为什么不用一次性 SELECT 全表 / SSCursor 流式游标
------------------------------------------------
标注 worker 正在往同一张表写回。开一个长事务的流式游标去读一张正在被大量 UPDATE 的表，
既会拉长 undo 链拖慢写入，也可能读到自己刚写的行。分批短读是这里更稳的做法。
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opinion_labeling_engine.config import AppConfig, load_config          # noqa: E402
from opinion_labeling_engine.logging_conf import get_logger, setup_logging  # noqa: E402
from opinion_labeling_engine.storage.mysql import MySQLPool                 # noqa: E402
from opinion_labeling_engine.worker.engine import LabelingEngine            # noqa: E402

logger = get_logger("label_from_table")

# 要查的六个字段，与引擎入参契约一一对应
SELECT_FIELDS = ["channel", "work_id", "scenic_id", "scenic_name", "comment_id", "content"]


# ===========================================================================
# 取数
# ===========================================================================
class CommentSource:
    """按批次从源表取待标注评论。

    读连接与引擎的写连接是两个独立的池：写侧在高并发 UPDATE 时会占满自己的池，
    读侧不该跟它抢连接。
    """

    def __init__(self, cfg: AppConfig, args: argparse.Namespace) -> None:
        self._cfg = cfg
        self._args = args
        self._pool = MySQLPool(cfg.mysql)
        self._table = cfg.storage.table
        self._label_col = cfg.storage.columns["sentiment_label"]
        self._score_col = cfg.storage.columns["sentiment_score"]
        self._flag_col = cfg.storage.review_column
        self._flags = cfg.storage.review_flags
        self._cursor_value: Optional[str] = args.resume_from or None
        self.duplicates_skipped = 0
        #: 本次跑批已经提交过的主键。抽干式取数如果某些行写不回去，
        #: 它们会一直匹配取数条件、每轮都被重新取出来重标——这是会烧钱的死循环，
        #: 靠这个集合识别出来并叫停。
        self._submitted_keys: set = set()
        self.repeats_across_batches = 0

        # 待标注的标记集合：命令行 > 配置
        self._pending_flags = list(args.pending_flags or cfg.storage.pending_flags)

        if args.mode == "failed" and not self._flag_col:
            raise SystemExit("--mode failed 需要配置 storage.review_column")

        self.use_cursor = self._decide_cursor_mode()

    def _decide_cursor_mode(self) -> bool:
        """判断取数要不要用游标翻页。

        抽干式取数（每轮 SELECT 待处理的前 N 条）成立的前提是
        **处理完之后这批行不再匹配条件**。一旦不成立，同一批会被反复取出来无限重标。

        什么时候不成立：待标注的标记集合里包含引擎自己会写的值。
        比如 pending_flags=[0,4]：引擎把标好的行写成 4，可 4 还在集合里，
        于是它下一轮又被取出来重标——这不是理论问题，是死循环。
        这种情况必须改用游标翻页，靠"游标只往前走"来保证每行只处理一次。
        """
        if self._args.mode == "all":
            return True
        if self._args.mode != "pending" or not self._flag_col:
            return False

        overlap = sorted(set(self._pending_flags) & set(self._cfg.storage.engine_written_flags()))
        if not overlap:
            return False

        logger.warning(
            "pending_flags=%s 里包含引擎自己会写的标记 %s —— 标完这些行仍然匹配取数条件，"
            "抽干式取数会无限重标同一批。已自动改用游标翻页（ORDER BY %s），"
            "每行只处理一次；中断后用 --resume-from 接着跑。",
            self._pending_flags, overlap, self._args.order_by)
        return True

    # ------------------------------------------------------------------ 条件
    def _filters(self) -> Tuple[List[str], List[Any]]:
        """拼公共 WHERE 条件。值一律走参数绑定，只有列名是拼进去的（来自校验过的配置）。"""
        where: List[str] = []
        params: List[Any] = []

        mode = self._args.mode
        if mode == "pending":
            if self._flag_col:
                marks = ", ".join(["%s"] * len(self._pending_flags))
                where.append(f"`{self._flag_col}` IN ({marks})")
                params.extend(self._pending_flags)
                # 标记为 0 的行还要额外要求标注字段是空的。
                # 因为标记列是后加的：历史上已经标过、但那时还没有这一列的行，
                # 标记也是 0，不能因为"标记=0"就把它们当成没标过再标一遍。
                # 其它标记（如 4）是引擎写的，标注字段必然有值，不需要这个条件。
                where.append(
                    f"(`{self._flag_col}` <> 0 OR "
                    f"`{self._label_col}` IS NULL OR `{self._label_col}` = '')")
            else:
                where.append(
                    f"(`{self._label_col}` IS NULL OR `{self._label_col}` = '')")
        elif mode == "failed":
            # 只重标 AI 标注失败的行
            where.append(f"`{self._flag_col}` = %s")
            params.append(int(self._flags.get("ai_failed", 5)))
        elif mode == "neutral":
            # 重标中性的行——清理历史上被兜底策略写成假中性的数据。
            # 注意：真正的无效内容（纯表情）和景区无关内容也是中性，长得一模一样，
            # 靠数据分不出来。好在重标它们几乎不花钱：纯表情的连模型都不调，
            # 结论也和上次一致，所以整批重标是安全的。
            where.append(f"`{self._score_col}` = 0")
            if self._flag_col and not self._args.include_reviewed:
                # 人工已经复核过的（1/2/3）不要动，那是人的结论，不该被 AI 覆盖
                human = [v for k, v in self._flags.items()
                         if k not in {"ai_success", "ai_failed", "ai_low_confidence"}]
                reviewed = sorted({1, 2, 3} | set(human))
                marks = ", ".join(["%s"] * len(reviewed))
                where.append(f"`{self._flag_col}` NOT IN ({marks})")
                params.extend(reviewed)

        if self._args.scenic_id:
            placeholders = ", ".join(["%s"] * len(self._args.scenic_id))
            where.append(f"`scenic_id` IN ({placeholders})")
            params.extend(self._args.scenic_id)

        if self._args.channel:
            placeholders = ", ".join(["%s"] * len(self._args.channel))
            where.append(f"`channel` IN ({placeholders})")
            params.extend(self._args.channel)

        if self._args.since:
            where.append("`publish_time` >= %s")
            params.append(self._args.since)

        if self._args.until:
            where.append("`publish_time` < %s")
            params.append(self._args.until)

        if self._args.where:
            # 调用方自己写的附加条件，不做转义——只允许运维在命令行里用
            where.append(f"({self._args.where})")

        if self._args.skip_empty_content:
            where.append("`content` IS NOT NULL AND `content` <> ''")
        # 默认不滤空内容：让它照常走一遍，被清洗判为无效后写成中性。
        # 在 SQL 层滤掉看着省事，但这些行的 sentiment_label 会永远是 NULL，
        # 下游取数时又要单独处理一次 NULL，等于把问题往后推。

        if not where:
            # --mode all 且不带任何过滤条件时 where 会是空的，
            # 补一个恒真条件，后面就可以无脑 " AND ".join 而不用判断有没有 WHERE
            where.append("1 = 1")
        return where, params

    # ------------------------------------------------------------------ 计数
    def count(self) -> int:
        """待处理总数，只用于打进度。表很大时这条 COUNT 本身也要几秒。"""
        where, params = self._filters()
        sql = f"SELECT COUNT(*) AS c FROM `{self._table}` WHERE " + " AND ".join(where)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
        return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ 取批
    def fetch_batch(self, size: int) -> List[Dict[str, Any]]:
        """取一批评论。返回空列表表示取完了。"""
        where, params = self._filters()
        order_sql = ""

        if self.use_cursor:
            col = self._args.order_by
            if self._cursor_value is not None:
                where.append(f"`{col}` > %s")
                params.append(self._cursor_value)
            order_sql = f" ORDER BY `{col}` ASC"

        cols = ", ".join(f"`{c}`" for c in SELECT_FIELDS)
        if self.use_cursor and self._args.order_by not in SELECT_FIELDS:
            cols += f", `{self._args.order_by}`"

        sql = (f"SELECT {cols} FROM `{self._table}` "
               f"WHERE " + " AND ".join(where) + order_sql + " LIMIT %s")
        params.append(int(size))

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())

        if rows and self.use_cursor:
            self._cursor_value = rows[-1][self._args.order_by]

        return self._dedupe(rows)

    def _dedupe(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按写回主键去重——既去批内重复，也去**跨批重复**。

        两种重复，成因不同但代价一样（每条都是一次白花的模型调用）：

        1. **批内重复**：源表里存在同一 (channel, scenic_id, comment_id) 的多行
           （补采、多任务重复入库）。写回按主键 UPDATE，一次就会把这组行全部更新，
           所以同一批里标第二次纯属浪费。

        2. **跨批重复**：某些行标完之后写不回去（主键对不上、行被删了……），
           它们仍然匹配取数条件，于是下一轮又被原样取出来。
           抽干式取数没有游标，``LIMIT n`` 每次都从扫描顺序的开头拿，
           这批行就会被**无限重标**——表现出来就是"跑了好几批，已标注数量却不再增长"。
           这里把它们识别出来，由调用方决定叫停。
        """
        if not self._args.dedupe or not rows:
            return rows

        seen_in_batch: set = set()
        out: List[Dict[str, Any]] = []
        for row in rows:
            key = tuple(str(row.get(c, "") or "") for c in self._cfg.storage.key_columns)
            if key in seen_in_batch:
                self.duplicates_skipped += 1
                continue
            seen_in_batch.add(key)

            if key in self._submitted_keys:
                self.repeats_across_batches += 1
                continue
            out.append(row)

        if self.duplicates_skipped:
            logger.debug("本批按写回主键去重，跳过重复行"
                         "（同一次 UPDATE 会把它们一起更新，不必重复标注）")
        return out

    def mark_submitted(self, rows: List[Dict[str, Any]]) -> None:
        """记下这一轮已经提交过的主键，供跨批重复识别。"""
        for row in rows:
            key = tuple(str(row.get(c, "") or "") for c in self._cfg.storage.key_columns)
            self._submitted_keys.add(key)

    @property
    def cursor_value(self) -> Optional[str]:
        """当前游标位置。中断后可以用 ``--resume-from`` 接着跑。"""
        return self._cursor_value

    def close(self) -> None:
        self._pool.close()


# ===========================================================================
# 跑批
# ===========================================================================
class Runner:
    """取数 → 入队 → 等写回 → 再取数。"""

    def __init__(self, cfg: AppConfig, args: argparse.Namespace) -> None:
        self._cfg = cfg
        self._args = args
        self._source = CommentSource(cfg, args)
        self._engine = LabelingEngine.from_config(
            args.config, enable_db=not args.dry_run, setup_log=False
        )
        self._stopping = False
        self._submitted = 0
        self._started_at = time.monotonic()
        self._stalled = False
        self._rows_for_csv: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ 信号
    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            if self._stopping:
                logger.warning("再次收到信号 %s，强制退出", signum)
                sys.exit(130)
            logger.warning("收到信号 %s，停止取数，等在途任务处理完……", signum)
            self._stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):        # pragma: no cover - 非主线程
                pass

    # ------------------------------------------------------------------ 主流程
    def run(self) -> int:
        args = self._args
        total = self._source.count()
        logger.info("待处理 %d 条（mode=%s，batch=%d，并发=%d%s）",
                    total, args.mode, args.batch_size, self._cfg.worker.concurrency,
                    "，干跑不写库" if args.dry_run else "")
        if total == 0:
            logger.info("没有需要标注的数据，退出")
            return 0
        if args.limit:
            logger.info("本次最多处理 %d 条", args.limit)

        self._engine.start(check_schema=not args.dry_run)

        try:
            self._loop(total)
        finally:
            if self._stalled:
                logger.warning("上一步已判定卡死，不再等待收尾")
            else:
                logger.info("停止取数，等待在途任务处理完"
                            "（不限时，只要还在推进就一直等）……")
                self._wait_until_done("收尾")
            leftover = self._engine.queue.stats()
            if leftover["pending"] or leftover["inflight"]:
                logger.warning(
                    "退出时队列里还有 %d 条待处理、%d 条在途，它们没有被标注。"
                    "这些行仍然匹配待标注条件，重跑一次会接着标，数据不会丢",
                    leftover["pending"], leftover["inflight"])
            self._engine.stop()
            self._source.close()

        self._report()
        self._dump_csv()
        return 0

    def _loop(self, total: int) -> None:
        args = self._args
        empty_rounds = 0

        while not self._stopping:
            remaining = (args.limit - self._submitted) if args.limit else args.batch_size
            if args.limit and remaining <= 0:
                logger.info("已达 --limit %d，停止取数", args.limit)
                break

            before_repeats = self._source.repeats_across_batches
            batch = self._source.fetch_batch(min(args.batch_size, max(1, remaining)))
            new_repeats = self._source.repeats_across_batches - before_repeats

            if not batch:
                if new_repeats:
                    self._abort_no_progress(new_repeats)
                else:
                    logger.info("已取完")
                break

            if new_repeats:
                logger.warning(
                    "本批有 %d 行是之前已经标过、但仍然匹配取数条件的——"
                    "说明它们没能写回去。已跳过，不再重复花钱标它们", new_repeats)

            self._source.mark_submitted(batch)
            accepted = self._engine.submit_many(batch)
            self._submitted += accepted
            self._log_progress(total, len(batch), accepted)

            # 干跑不写库，pending 模式的过滤条件不会收缩，再取一次还是同一批 → 只跑一轮
            if args.dry_run:
                logger.info("干跑模式只处理一批，如需更多请调大 --batch-size 或 --limit")
                break

            # 抽干式取数（非游标）依赖"处理完就不再匹配条件"，
            # 所以必须等这批写回完成，否则下一次 SELECT 还会取到同一批
            if not self._source.use_cursor:
                if not self._wait_until_done("等待本批写回"):
                    break
                # 全批都没入队成功（比如整批缺主键），再取一次还是它们 —— 防死循环
                if accepted == 0:
                    empty_rounds += 1
                    if empty_rounds >= 3:
                        logger.error("连续 3 批数据全部无法入队，停止。"
                                     "请检查 channel / scenic_id / comment_id 是否有空值")
                        break
                else:
                    empty_rounds = 0
            else:
                # 游标模式不用等写回（游标只往前走，不会重复取）；
                # 但**必须**做背压，否则取数比标注快几个数量级，
                # 几万条会瞬间灌满内存队列，而队列里的任务在停机时是保不住的
                self._apply_backpressure()

    def _wait_until_done(self, what: str) -> bool:
        """等在途任务处理完。**不设总时长上限**——只要还在推进就一直等。

        原来这里用的是固定超时（drain_timeout）。那是错的：
        五万条数据按每条几秒算要跑十几个小时，固定 30 分钟必然超时，
        然后 stop() 把队列里几万条没处理的任务直接丢掉——
        表现出来就是"跑了一小时、只标了两千条就结束了"。

        判断依据换成「有没有进展」：完成数还在涨就继续等；
        连续 ``--stall-timeout`` 秒一条都没完成，才认定卡死并停下。

        Returns:
            True 表示队列已排空；False 表示卡住了（调用方应停止取数）。
        """
        last_done = -1
        last_change = time.monotonic()
        stall = self._args.stall_timeout

        while True:
            if self._engine.wait_idle(timeout=15.0):
                return True

            stats = self._engine.stats()
            done, pending = stats["processed"], stats["pending"]

            if done != last_done:
                last_done = done
                last_change = time.monotonic()
                logger.info("%s：已完成 %d，队列剩余 %d，在途 %d",
                            what, done, pending, stats["inflight"])
                continue

            idle_for = time.monotonic() - last_change
            if idle_for >= stall:
                self._stalled = True
                logger.error(
                    "%s：已经 %.0f 秒没有任何一条完成，队列里还压着 %d 条。"
                    "判定为卡死并停止等待——这些行没有被标注，重跑一次会接着标。"
                    "常见原因：模型接口挂了、Redis/MySQL 断了。看上面的 ERROR 日志。",
                    what, idle_for, pending)
                return False

    def _abort_no_progress(self, repeats: int) -> None:
        """整批都是"标过但还在待标注列表里"的行 —— 取数没有推进，必须停。

        再跑下去只是把同一批数据反复送进模型，钱花了、数据一行没多。
        典型表现就是"跑了好几批，表里已标注的数量却卡在某个值不动"。
        """
        logger.error(
            "取数没有推进：本轮取到的 %d 行全都是之前已经标注过、"
            "但至今仍匹配待标注条件的行——它们的标注结果没有写回数据库。\n"
            "  常见原因（按可能性排序）：\n"
            "    1. 写回主键对不上：日志里若有『未命中』，看它的诊断输出，"
            "通常是 channel/scenic_id/comment_id 有前后空格或大小写差异\n"
            "    2. 取数和写回连的不是同一个库/表\n"
            "    3. label_review_flag 列不存在或写不进去（检查 storage.review_column）\n"
            "  排查语句见 sql/optional_review_column.sql 末尾。已停止跑批，避免继续空烧模型调用。",
            repeats)

    def _apply_backpressure(self) -> None:
        """队列积压太多就先别取数，免得把内存/Redis 撑爆。"""
        high = self._args.queue_high_water
        while not self._stopping:
            pending = self._engine.queue.stats()["pending"]
            if pending < high:
                return
            logger.debug("队列积压 %d ≥ %d，暂停取数 1s", pending, high)
            time.sleep(1.0)

    # ------------------------------------------------------------------ 输出
    def _log_progress(self, total: int, fetched: int, accepted: int) -> None:
        elapsed = max(0.001, time.monotonic() - self._started_at)
        stats = self._engine.stats()
        done = stats["processed"]
        rate = done / elapsed
        eta = (total - done) / rate if rate > 0 else 0
        logger.info(
            "取 %d 条 / 入队 %d 条 | 已完成 %d/%d (%.1f%%) | %.1f 条/秒 | 预计剩余 %s | "
            "队列 %d 在途 %d 死信 %d",
            fetched, accepted, done, total, 100.0 * done / max(1, total), rate,
            _fmt_duration(eta), stats["pending"], stats["inflight"], stats["dead"],
        )

    def _report(self) -> None:
        stats = self._engine.stats()
        elapsed = time.monotonic() - self._started_at
        # 同步干跑不启 worker，processed 恒为 0，这时用标注服务自己的计数
        done = stats["processed"] or stats["total"]
        logger.info("=" * 60)
        logger.info("跑批结束，耗时 %s", _fmt_duration(elapsed))
        logger.info("提交 %d | 完成 %d | 走模型 %d | 无效内容判中性 %d | "
                    "景区无关判中性 %d | 解析修复 %d | 失败兜底 %d | 低置信度 %d",
                    stats["submitted"], done, stats["labeled"],
                    stats["skipped_invalid"], stats["irrelevant"],
                    stats["parse_repaired"], stats["failed"], stats["low_confidence"])
        if stats["dead"]:
            logger.warning("死信 %d 条，需要人工排查（queue.drain_dead() 可取出补跑）",
                           stats["dead"])
        if stats["dropped_on_submit"]:
            logger.warning("入队被拒 %d 条（缺主键字段），这些行没有被标注",
                           stats["dropped_on_submit"])
        repo = getattr(self._engine, "repository", None)
        dup_writes = getattr(repo, "duplicate_writes", 0) if repo else 0
        if dup_writes:
            logger.warning("重复写回 %d 条：同一条评论在本次跑批里被标了不止一次，"
                           "每一次重复都是一次白花的模型调用", dup_writes)
        if self._source.duplicates_skipped:
            logger.info("按写回主键跳过同键重复行 %d 条（省下同样多次模型调用）",
                        self._source.duplicates_skipped)
        if self._source.repeats_across_batches:
            logger.warning(
                "跳过「标过但仍匹配待标注条件」的行 %d 条 —— 这些行的标注没写回数据库，"
                "先按上面的提示排查写回主键，再重跑",
                self._source.repeats_across_batches)
        if self._args.mode == "all" and self._source.cursor_value:
            logger.info("游标停在 %s；中断后可用 --resume-from '%s' 接着跑",
                        self._source.cursor_value, self._source.cursor_value)
        logger.info("=" * 60)

    def _dump_csv(self) -> None:
        """干跑模式把结果落 CSV，方便人工抽验。"""
        if not self._args.out or not self._rows_for_csv:
            return
        path = Path(self._args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(self._rows_for_csv[0].keys()))
            writer.writeheader()
            writer.writerows(self._rows_for_csv)
        logger.info("结果已写入 %s（%d 行）", path, len(self._rows_for_csv))

    # ------------------------------------------------------------------ 干跑
    def run_dry_sync(self) -> int:
        """干跑：同步逐条标注，直接打印结果，不入队不写库。

        条数少的时候比走队列更直观——出错立刻看到，顺序也和取数一致。
        """
        args = self._args
        rows = self._source.fetch_batch(args.limit or args.batch_size)
        logger.info("取到 %d 条，开始干跑标注", len(rows))

        for idx, row in enumerate(rows, start=1):
            if self._stopping:
                break
            result = self._engine.label_sync(row)
            print(f"[{idx}/{len(rows)}] {row.get('content', '')[:40]}")
            print(f"        {result.summary()}")
            if result.dropped:
                print(f"        丢弃：{json.dumps(result.dropped, ensure_ascii=False)}")
            self._rows_for_csv.append({
                "channel": row.get("channel", ""),
                "scenic_id": row.get("scenic_id", ""),
                "comment_id": row.get("comment_id", ""),
                "content": row.get("content", ""),
                "sentiment_label": result.sentiment_label,
                "sentiment_score": result.sentiment_score,
                "dimension_tags": result.dimension_json(),
                "entity_tags": result.entity_json(),
                "keyword_tags": result.keyword_json(),
                "confidence": result.confidence,
                "source": result.source.value,
                "reason": result.reason,
            })

        self._report()
        self._dump_csv()
        self._engine.service.close()
        self._source.close()
        return 0


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds // 3600}小时{(seconds % 3600) // 60}分"


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从源表读评论跑标注引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-c", "--config", help="配置文件路径，默认 config/config.yaml")

    g = p.add_argument_group("取数范围")
    g.add_argument("--mode", choices=["pending", "failed", "neutral", "all"],
                   default="pending",
                   help="pending=只标未标注的（默认，已标注的不再动）；"
                        "failed=只重标 AI 标注失败的（label_review_flag=5）；"
                        "neutral=重标中性的（清理历史兜底假数据）；"
                        "all=全量重标，需配 --order-by")
    g.add_argument("--pending-flags", type=int, nargs="+", default=None, metavar="N",
                   help="pending 模式下哪些 label_review_flag 算待标注，"
                        "默认取配置里的 storage.pending_flags（[0]）。"
                        "例：--pending-flags 0 4 连 AI 标过的一起重标"
                        "（会自动切游标翻页，否则无限重标）")
    g.add_argument("--include-reviewed", action="store_true",
                   help="neutral 模式下连人工已复核的行（标记 1/2/3）也一起重标。"
                        "默认不动——那是人的结论，不该被 AI 覆盖")
    g.add_argument("--scenic-id", nargs="+", default=[], help="只标这些景区ID，可给多个")
    g.add_argument("--channel", nargs="+", default=[], help="只标这些渠道，可给多个")
    g.add_argument("--since", help="publish_time >= 这个时间，如 2026-08-01")
    g.add_argument("--until", help="publish_time < 这个时间")
    g.add_argument("--where", default="", help="附加 SQL 条件，原样拼进 WHERE")
    g.add_argument("--no-dedupe", dest="dedupe", action="store_false", default=True,
                   help="不按写回主键去重。默认去重：源表若有同键重复行，"
                        "一次 UPDATE 就会把它们全部更新，重复标注是白花钱")
    g.add_argument("--skip-empty-content", action="store_true",
                   help="跳过 content 为空的行（默认不跳，让它们被标成中性，"
                        "免得 sentiment_label 永远留 NULL）")
    g.add_argument("--limit", type=int, default=0, help="本次最多处理多少条，0=不限")
    g.add_argument("--order-by", default="comment_id",
                   help="all 模式的游标列，必须唯一且可排序（默认 comment_id）")
    g.add_argument("--resume-from", help="all 模式断点续跑：从这个游标值之后开始")

    g = p.add_argument_group("运行参数")
    g.add_argument("--batch-size", type=int, default=500, help="每批取多少条（默认 500）")
    g.add_argument("--queue-high-water", type=int, default=2000,
                   help="队列积压超过这个数就暂停取数（默认 2000）")
    g.add_argument("--stall-timeout", type=float, default=600.0,
                   help="连续多少秒一条都没完成就判定卡死并停止（默认 600）。"
                        "注意这是『没有进展』的时长，不是总时长——"
                        "只要还在往前跑，跑多久都不会被打断")
    g.add_argument("--drain-timeout", type=float, default=1800.0,
                   help="（已废弃，保留兼容）改用 --stall-timeout")

    p.add_argument("--diagnose", action="store_true",
                   help="只体检不标注：打印总量/去重量/各标记分布/待标注数，"
                        "以及一条待标注行能否被自己的写回 SQL 命中")

    g = p.add_argument_group("干跑")
    g.add_argument("--dry-run", action="store_true", help="不写库，只打印结果")
    g.add_argument("--sync", action="store_true",
                   help="配合 --dry-run：同步逐条标注，结果按取数顺序打印")
    g.add_argument("--out", help="结果输出 CSV 路径（干跑时有用）")

    return p


def diagnose(cfg: AppConfig, args: argparse.Namespace) -> int:
    """只读体检：把"提交数和实际标注数对不上"这件事拆成可核对的数字。"""
    source = CommentSource(cfg, args)
    table = cfg.storage.table
    keys = cfg.storage.key_columns
    flag_col = cfg.storage.review_column
    label_col = cfg.storage.columns["sentiment_label"]
    key_expr = ", ".join(f"`{c}`" for c in keys)

    def one(sql: str, params=()):
        with source._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    print("=" * 68)
    print(f"表：{table}    写回主键：{keys}")
    print("=" * 68)

    # 用子查询而不是 COUNT(DISTINCT a,b,c)：后者是 MySQL 方言，
    # 这样写在 MySQL 和单测用的 sqlite 上语义一致，能被真正测到
    row = one(
        f"SELECT (SELECT COUNT(*) FROM `{table}`) AS total, "
        f"       (SELECT COUNT(*) FROM (SELECT 1 FROM `{table}` "
        f"        GROUP BY {key_expr}) k) AS distinct_keys"
    )[0]
    total, distinct = int(row["total"]), int(row["distinct_keys"])
    print(f"总行数            {total}")
    print(f"不同主键数        {distinct}")
    if total != distinct:
        print(f"  ⚠ 有 {total - distinct} 行是重复主键。写回按主键 UPDATE 会一次更新一整组，"
              f"所以\"提交数\"天然大于\"新增标注行数\"")

    labeled = one(f"SELECT COUNT(*) AS c FROM `{table}` "
                  f"WHERE `{label_col}` IS NOT NULL AND `{label_col}` <> ''")[0]["c"]
    print(f"已标注（有情感）  {labeled}")

    if flag_col:
        print(f"\n{flag_col} 分布：")
        meaning = {0: "未标注", 1: "人工复核正确", 2: "人工复核错误", 3: "复核成功",
                   4: "AI标注成功", 5: "AI标注错误", 6: "未人工复核"}
        for r in one(f"SELECT `{flag_col}` AS f, COUNT(*) AS c FROM `{table}` "
                     f"GROUP BY `{flag_col}` ORDER BY 1"):
            print(f"  {r['f']}  {meaning.get(int(r['f']), '?'):<12} {r['c']}")

    pending = source.count()
    print(f"\n本次配置下待标注    {pending} 条"
          f"（mode={args.mode}, pending_flags={source._pending_flags}, "
          f"游标翻页={'是' if source.use_cursor else '否'}）")

    # 关键一步：拿一条待标注的行，验证它能不能被自己的写回 SQL 命中
    sample = source.fetch_batch(1)
    if not sample:
        print("\n没有待标注数据，体检结束")
        source.close()
        return 0

    row = sample[0]
    where = " AND ".join(f"`{c}` = %s" for c in keys)
    params = tuple(str(row.get(c, "") or "") for c in keys)
    hit = one(f"SELECT COUNT(*) AS c FROM `{table}` WHERE {where}", params)[0]["c"]
    print(f"\n写回连通性自检（comment_id={row.get('comment_id')}）：")
    print(f"  按主键回查命中 {hit} 行", "✅ 写回没问题" if hit else "❌ 查不到！写回必然失败")
    if not hit:
        print("  → 主键值取出来又查不回去，检查前后空格与大小写："
              "utf8mb4_0900_ai_ci 是 NO PAD 排序规则，尾部空格参与比较")
        for c in keys:
            print(f"     {c} = {str(row.get(c, ''))!r}")

    source.close()
    return 0 if hit else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = load_config(args.config, require_db=True)   # 读库是刚需，一定要校验连接配置
    setup_logging(cfg.logging)

    if args.diagnose:
        return diagnose(cfg, args)

    if args.mode == "all" and not args.order_by:
        print("--mode all 必须指定 --order-by（唯一且可排序的列）")
        return 1
    if args.sync and not args.dry_run:
        print("--sync 只在 --dry-run 下有意义")
        return 1
    if args.out and not args.dry_run:
        print("--out 只在 --dry-run 下有意义：正式跑批的结果直接写回数据库")
        return 1
    if args.out and not args.sync:
        # 异步路径拿不到逐条结果（结果在 worker 里就写库了），要出 CSV 只能走同步
        logger.info("--out 需要逐条结果，已自动切到 --sync 同步标注")
        args.sync = True

    runner = Runner(cfg, args)
    runner.install_signal_handlers()

    if args.dry_run and args.sync:
        return runner.run_dry_sync()
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
