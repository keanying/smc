#!/usr/bin/env python3
"""回填 aggregation_keyword_tags —— 把已有的 keyword_tags 按词典归并。

**一次模型调用都不需要。** 归类是纯字典映射，本地算完直接 UPDATE，
所以已经标好的几万条可以免费补上归类关键词；改了 config/aggregation.yaml
之后重跑一次，历史数据跟着一起变。

用法
----
    # 先看看会归成什么样，不写库
    python scripts/backfill_aggregation.py --dry-run

    # 看哪些关键词还没归类上（频次倒序）——这是扩词典的依据
    python scripts/backfill_aggregation.py --report

    # 真回填
    python scripts/backfill_aggregation.py

    # 只回填还没归类过的行（默认回填全部有 keyword_tags 的行）
    python scripts/backfill_aggregation.py --only-missing

取数用游标翻页（按 comment_id），不是抽干式：
回填后的行**仍然匹配取数条件**（keyword_tags 还在），抽干式会无限重跑同一批。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opinion_labeling_engine.config import AppConfig, ConfigError, load_config  # noqa: E402
from opinion_labeling_engine.domain.aggregation import KeywordAggregator        # noqa: E402
from opinion_labeling_engine.logging_conf import get_logger, setup_logging      # noqa: E402
from opinion_labeling_engine.storage.mysql import MySQLPool                     # noqa: E402

logger = get_logger("backfill_aggregation")


def parse_keywords(raw: Any) -> List[str]:
    """把库里的 keyword_tags 解析成字符串列表。

    历史数据可能是 JSON 数组、也可能是被写成字符串的 JSON，甚至是脏数据。
    解析不了就当空列表——回填不该因为一条脏数据中断。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if str(x).strip()]
    return []


class Backfiller:
    def __init__(self, cfg: AppConfig, args: argparse.Namespace) -> None:
        self._cfg = cfg
        self._args = args
        self._pool = MySQLPool(cfg.mysql)
        self._agg = KeywordAggregator.load(
            Path(args.dict_path) if args.dict_path else cfg.aggregation_path)

        self._table = cfg.storage.table
        self._keys = list(cfg.storage.key_columns)
        self._kw_col = cfg.storage.columns["keyword_tags"]
        self._agg_col = cfg.storage.columns.get("aggregation_keyword_tags")
        if not self._agg_col:
            raise SystemExit(
                "config.yaml 的 storage.columns 里没有 aggregation_keyword_tags。"
                "先执行 sql/aggregation_keyword_tags.sql 加列，再把映射加回配置")

        self.scanned = 0
        self.aggregated = 0
        self.empty = 0          # 有关键词但一个都没归类上
        self.written = 0
        self._cursor: Optional[str] = args.resume_from

    # ------------------------------------------------------------------ 取数
    def _where(self) -> Tuple[List[str], List[Any]]:
        where = [f"`{self._kw_col}` IS NOT NULL", f"`{self._kw_col}` <> '[]'"]
        params: List[Any] = []

        if self._args.only_missing:
            where.append(f"(`{self._agg_col}` IS NULL OR `{self._agg_col}` = '[]'"
                         f" OR `{self._agg_col}` = '')")
        if self._args.scenic_id:
            marks = ", ".join(["%s"] * len(self._args.scenic_id))
            where.append(f"`scenic_id` IN ({marks})")
            params.extend(self._args.scenic_id)
        if self._args.where:
            where.append(f"({self._args.where})")
        return where, params

    def count(self) -> int:
        where, params = self._where()
        sql = f"SELECT COUNT(*) AS c FROM `{self._table}` WHERE " + " AND ".join(where)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return int((cur.fetchone() or {}).get("c", 0))

    def fetch(self, size: int) -> List[Dict[str, Any]]:
        where, params = self._where()
        if self._cursor is not None:
            where.append("`comment_id` > %s")
            params.append(self._cursor)

        cols = ", ".join(f"`{c}`" for c in dict.fromkeys(self._keys + ["comment_id"]))
        sql = (f"SELECT {cols}, `{self._kw_col}` AS kw FROM `{self._table}` "
               f"WHERE " + " AND ".join(where) +
               " ORDER BY `comment_id` ASC LIMIT %s")
        params.append(int(size))

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())
        if rows:
            self._cursor = rows[-1]["comment_id"]
        return rows

    # ------------------------------------------------------------------ 写回
    def write(self, updates: List[Tuple]) -> int:
        if not updates or self._args.dry_run:
            return 0
        where = " AND ".join(f"`{c}` = %s" for c in self._keys)
        sql = f"UPDATE `{self._table}` SET `{self._agg_col}` = %s WHERE {where}"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                affected = cur.executemany(sql, updates)
            conn.commit()
        return int(affected if affected is not None else len(updates))

    # ------------------------------------------------------------------ 主流程
    def run(self) -> int:
        total = self.count()
        logger.info("待回填 %d 行（词典 %d 组归类词%s）",
                    total, len(self._agg), "，干跑不写库" if self._args.dry_run else "")
        if not total:
            return 0

        started = time.monotonic()
        while True:
            if self._args.limit and self.scanned >= self._args.limit:
                break
            size = self._args.batch_size
            if self._args.limit:
                size = min(size, self._args.limit - self.scanned)

            rows = self.fetch(size)
            if not rows:
                break

            updates: List[Tuple] = []
            for row in rows:
                self.scanned += 1
                keywords = parse_keywords(row.get("kw"))
                tags = self._agg.aggregate(keywords)

                if tags:
                    self.aggregated += 1
                elif keywords:
                    self.empty += 1

                if self._args.dry_run and self.scanned <= self._args.show:
                    print(f"  {keywords} → {tags or '（未归类）'}")

                payload = json.dumps(tags, ensure_ascii=self._cfg.storage.json_ensure_ascii)
                updates.append(tuple([payload] + [row.get(c, "") for c in self._keys]))

            self.written += self.write(updates)
            rate = self.scanned / max(0.001, time.monotonic() - started)
            logger.info("已扫描 %d/%d，归类成功 %d，写回 %d，%.0f 行/秒",
                        self.scanned, total, self.aggregated, self.written, rate)

        self._report()
        return 0

    def _report(self) -> None:
        logger.info("=" * 60)
        logger.info("扫描 %d 行｜归类到至少一个词 %d 行｜有关键词但一个没归上 %d 行｜写回 %d 行",
                    self.scanned, self.aggregated, self.empty, self.written)
        if self._args.dry_run:
            logger.info("（干跑，没有写库）")

        unmatched = self._agg.top_unmatched(self._args.top)
        if unmatched:
            logger.info("-" * 60)
            logger.info("没归类上的关键词 TOP %d —— 频次高的就该补进 config/aggregation.yaml：",
                        len(unmatched))
            for word, count in unmatched:
                logger.info("  %6d  %s", count, word)
        logger.info("=" * 60)

    def close(self) -> None:
        self._pool.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="回填归类关键词（不调模型）",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("-c", "--config", help="配置文件路径")
    p.add_argument("--dict-path", help="聚合词典路径，默认 config/aggregation.yaml")
    p.add_argument("--only-missing", action="store_true",
                   help="只回填还没归类过的行（默认回填全部有 keyword_tags 的行，"
                        "这样改了词典之后能整体刷新）")
    p.add_argument("--scenic-id", nargs="+", default=[], help="只回填这些景区")
    p.add_argument("--where", default="", help="附加 SQL 条件")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少行，0=全部")
    p.add_argument("--batch-size", type=int, default=2000, help="每批多少行（默认 2000）")
    p.add_argument("--resume-from", help="从这个 comment_id 之后接着跑")
    p.add_argument("--dry-run", action="store_true", help="不写库，只看归类效果")
    p.add_argument("--show", type=int, default=30, help="干跑时打印前几条对照")
    p.add_argument("--top", type=int, default=40, help="报告里列出多少个未归类词")
    p.add_argument("--report", action="store_true",
                   help="只统计未归类词，不写库（等价于 --dry-run --show 0）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report:
        args.dry_run = True
        args.show = 0

    try:
        cfg = load_config(args.config, require_db=True)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(cfg.logging)

    worker = Backfiller(cfg, args)
    try:
        return worker.run()
    except KeyboardInterrupt:
        logger.warning("已中断。用 --resume-from %s 接着跑", worker._cursor)
        return 130
    finally:
        worker.close()


if __name__ == "__main__":
    sys.exit(main())
