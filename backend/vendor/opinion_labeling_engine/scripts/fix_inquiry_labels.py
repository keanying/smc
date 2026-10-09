#!/usr/bin/env python3
"""修正已经标好的「咨询提问类」评论 —— 一次模型调用都不需要。

为什么需要它
------------
规则是这次才加的，但库里已经有几万条按老规则标过的数据，里面混着：

    最近人多吗              sentiment=负向  keyword_tags=["人多"]
    人多吗？我们4岁多可去么   sentiment=负向  keyword_tags=["人多","4岁多"]

这些"人多"会被聚合成「人多拥挤」，直接拉高景区的拥挤度指标——
而说这话的人压根还没去过。重标一遍要花模型钱，但**这件事不用**：
判断一条评论是不是纯提问、一个关键词是不是数量词，全是确定性规则，
拿库里已有的 content 就能在本地算出来，算完直接 UPDATE。

它做什么
--------
对每一行（content + keyword_tags 都在库里）重新跑一遍规则层：

    1. 整条都是提问   → sentiment 改中性(0)、dimension_tags / keyword_tags /
                        aggregation_keyword_tags 全部清空
    2. 半问半评       → 只把落在疑问小句里的关键词摘掉，情感与维度不动
    3. 纯数量词       → 摘掉（4岁多 / 两小时 / 50块）
    4. 关键词有变化   → 重算 aggregation_keyword_tags

**只改需要改的行**：算完和原值一样就不写，所以可以放心重复跑。

安全边界
--------
- 人工复核过的行（label_review_flag 1/2/3）默认跳过，AI 不覆盖人的结论
- 只 UPDATE，绝不 INSERT
- --dry-run 先看会改多少、改成什么样

用法
----
    python scripts/fix_inquiry_labels.py --dry-run     # 先看影响面
    python scripts/fix_inquiry_labels.py               # 真修
    python scripts/fix_inquiry_labels.py --scenic-id PFTSCA01009835
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
from opinion_labeling_engine.domain.inquiry import (                            # noqa: E402
    declarative_text,
    interrogative_spans,
    is_pure_inquiry,
    is_quantity_only,
    occurrences_all_inside,
)
from opinion_labeling_engine.logging_conf import get_logger, setup_logging      # noqa: E402
from opinion_labeling_engine.storage.mysql import MySQLPool                     # noqa: E402

logger = get_logger("fix_inquiry_labels")


def parse_list(raw: Any) -> List[Any]:
    """把库里的 JSON 数组字段解析成 list；脏数据一律当空，不让它中断整批。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    text = str(raw).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def clean_keywords(keywords: List[str], content: str) -> Tuple[List[str], List[str]]:
    """按疑问/数量规则过滤关键词。

    Returns:
        (保留的, 摘掉的)
    """
    spans = interrogative_spans(content)
    rest = declarative_text(content) if spans else content

    kept, dropped = [], []
    for word in keywords:
        word = str(word).strip()
        if not word:
            continue
        if is_quantity_only(word):
            dropped.append(word)
            continue
        # 只在疑问小句里出现过、且陈述部分也找不到 → 是被打听的对象，不是评价
        if spans and occurrences_all_inside(word, content, spans) and word not in rest:
            dropped.append(word)
            continue
        kept.append(word)
    return kept, dropped


class InquiryFixer:
    def __init__(self, cfg: AppConfig, args: argparse.Namespace) -> None:
        self._cfg = cfg
        self._args = args
        self._pool = MySQLPool(cfg.mysql)
        self._agg = KeywordAggregator.load(cfg.aggregation_path)

        cols = cfg.storage.columns
        self._table = cfg.storage.table
        self._keys = list(cfg.storage.key_columns)
        self._c_label = cols["sentiment_label"]
        self._c_score = cols["sentiment_score"]
        self._c_dim = cols["dimension_tags"]
        self._c_kw = cols["keyword_tags"]
        self._c_agg = cols.get("aggregation_keyword_tags")
        self._review = cfg.storage.review_column

        self.scanned = 0
        self.pure_inquiry = 0        # 整条提问，被改成中性
        self.partial = 0             # 半问半评，只摘了关键词
        self.written = 0
        self.samples: List[str] = []
        self._cursor: Optional[str] = args.resume_from

    # ------------------------------------------------------------------ 取数
    def _where(self) -> Tuple[List[str], List[Any]]:
        # 只看已经标过、且抽出过关键词的行——没关键词的行没什么可修的
        where = [f"`{self._c_kw}` IS NOT NULL", f"`{self._c_kw}` <> '[]'",
                 "`content` IS NOT NULL", "`content` <> ''"]
        params: List[Any] = []

        if self._review and not self._args.include_reviewed:
            # 人工复核过的（1 正确 / 2 错误 / 3 复核成功）不碰，AI 不覆盖人
            where.append(f"(`{self._review}` IS NULL OR `{self._review}` NOT IN (1, 2, 3))")
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

        cols = list(dict.fromkeys(self._keys + ["comment_id", "content"]))
        select = ", ".join(f"`{c}`" for c in cols)
        extra = f", `{self._c_agg}` AS agg" if self._c_agg else ""
        sql = (f"SELECT {select}, `{self._c_kw}` AS kw, `{self._c_dim}` AS dim, "
               f"`{self._c_score}` AS score{extra} "
               f"FROM `{self._table}` WHERE " + " AND ".join(where) +
               " ORDER BY `comment_id` ASC LIMIT %s")
        params.append(int(size))

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())
        if rows:
            self._cursor = rows[-1]["comment_id"]
        return rows

    # ------------------------------------------------------------------ 计算
    def plan(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """算出这一行该改成什么样；不需要改返回 None。"""
        content = str(row.get("content") or "")
        keywords = [str(k) for k in parse_list(row.get("kw"))]
        if not content or not keywords:
            return None

        ensure_ascii = self._cfg.storage.json_ensure_ascii

        if is_pure_inquiry(content):
            # 整条都是提问：没有评价可言，情感、维度、关键词全部归零
            self.pure_inquiry += 1
            new = {
                self._c_label: "中性",
                self._c_score: 0,
                self._c_dim: "[]",
                self._c_kw: "[]",
            }
            if self._c_agg:
                new[self._c_agg] = "[]"
            changed = (int(row.get("score") or 0) != 0
                       or parse_list(row.get("kw")) != []
                       or parse_list(row.get("dim")) != [])
            note = f"[纯提问] {content[:28]} | {keywords} → []"
        else:
            kept, dropped = clean_keywords(keywords, content)
            if not dropped:
                return None
            self.partial += 1
            new = {self._c_kw: json.dumps(kept, ensure_ascii=ensure_ascii)}
            if self._c_agg:
                new[self._c_agg] = json.dumps(self._agg.aggregate(kept, track_unmatched=False),
                                              ensure_ascii=ensure_ascii)
            changed = True
            note = f"[摘词]   {content[:28]} | 摘掉 {dropped} → {kept}"

        if not changed:
            return None
        if len(self.samples) < self._args.show:
            self.samples.append(note)
        return new

    # ------------------------------------------------------------------ 写回
    def write(self, updates: List[Tuple[Tuple[str, ...], Dict[str, Any], Tuple]]) -> int:
        """按"要改的列组合"分组批量 UPDATE。

        纯提问行和摘词行要改的列不一样，分组之后每组一条 executemany，
        比逐行 UPDATE 快一个数量级。
        """
        if not updates or self._args.dry_run:
            return 0

        buckets: Dict[Tuple[str, ...], List[Tuple]] = {}
        for cols, values, key in updates:
            buckets.setdefault(cols, []).append(
                tuple([values[c] for c in cols]) + key)

        affected = 0
        where = " AND ".join(f"`{c}` = %s" for c in self._keys)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                for cols, params in buckets.items():
                    setter = ", ".join(f"`{c}` = %s" for c in cols)
                    cur.executemany(
                        f"UPDATE `{self._table}` SET {setter} WHERE {where}", params)
                    affected += int(cur.rowcount or 0)
            conn.commit()
        return affected

    # ------------------------------------------------------------------ 主流程
    def run(self) -> int:
        total = self.count()
        logger.info("待检查 %d 行%s", total, "（干跑，不写库）" if self._args.dry_run else "")
        if not total:
            logger.info("没有需要检查的行")
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

            updates = []
            for row in rows:
                self.scanned += 1
                new = self.plan(row)
                if not new:
                    continue
                key = tuple(row.get(k, "") for k in self._keys)
                updates.append((tuple(sorted(new)), new, key))

            self.written += self.write(updates)
            rate = self.scanned / max(0.001, time.monotonic() - started)
            logger.info("已扫描 %d/%d，纯提问 %d，摘词 %d，写回 %d，%.0f 行/秒",
                        self.scanned, total, self.pure_inquiry, self.partial,
                        self.written, rate)

        self._report()
        return 0

    def _report(self) -> None:
        logger.info("=" * 66)
        logger.info("扫描 %d 行", self.scanned)
        logger.info("  整条是咨询提问，改为中性并清空标签：%d 行", self.pure_inquiry)
        logger.info("  半问半评，只摘掉疑问句里的词/数量词：%d 行", self.partial)
        logger.info("  实际写回：%d 行", self.written)
        if self.samples:
            logger.info("-" * 66)
            logger.info("样例：")
            for note in self.samples:
                logger.info("  %s", note)
        if self._args.dry_run:
            logger.info("（干跑，没有写库；去掉 --dry-run 执行）")
        logger.info("=" * 66)

    def close(self) -> None:
        self._pool.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="修正咨询提问类评论的历史标注（不调模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-c", "--config", help="配置文件路径")
    p.add_argument("--scenic-id", nargs="+", default=[], help="只处理这些景区")
    p.add_argument("--where", default="", help="附加 SQL 条件")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少行，0=全部")
    p.add_argument("--batch-size", type=int, default=2000, help="每批多少行")
    p.add_argument("--resume-from", help="从这个 comment_id 之后接着跑")
    p.add_argument("--dry-run", action="store_true", help="不写库，只看会改哪些")
    p.add_argument("--show", type=int, default=30, help="打印多少条改动样例")
    p.add_argument("--include-reviewed", action="store_true",
                   help="连人工复核过的行（flag 1/2/3）一起改。默认不碰，AI 不覆盖人")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, require_db=True)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 1
    setup_logging(cfg.logging)

    worker = InquiryFixer(cfg, args)
    try:
        return worker.run()
    except KeyboardInterrupt:
        logger.warning("已中断。用 --resume-from %s 接着跑", worker._cursor)
        return 130
    finally:
        worker.close()


if __name__ == "__main__":
    sys.exit(main())
