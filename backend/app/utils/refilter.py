# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""按景区**现在的**关键字，把库里已有的作品重新过一遍第二、三道关卡，不相关的连评论一起删掉。

用在：改了主关键字 / 附关键字 / 过滤关键字之后，之前按老词表采进来的无关作品要清掉。
判定和采集时完全一样（scheduler/runner.py 的 _work_text + core/content_filter.py）：

    文本 = 作品的标题 + 描述 + 标签
    ② 含任一**启用的**主关键字或附关键字 → 相关；都不含 → 删（原因「不含关键字」）
    ③ 相关的再看过滤关键字，命中任一 → 删（原因「命中过滤词 X」）

只看抖音 / 快手 / 小红书 / 微博的作品：携程 / 同程 / 去哪儿的「作品」是景区点评的挂载点，不动。
按作者主页、作品链接采的（没有搜索关键字的）默认不动：它们采集时本来就不过关键字这一关。

命令行入口：scripts/refilter_works.py（默认只预览，加 --apply 才删）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.constants import CHANNELS_WITH_WORKS
from ..core.content_filter import MODE_KEYWORD_PLUS, ContentFilter
from ..core.db import Database
from ..core.logging import get_logger
from ..db import tables as T

logger = get_logger(__name__)

BATCH = 500


def work_text(row: Dict[str, Any]) -> str:
    """和采集时 runner._work_text 一样：标题 + 描述 + 标签。"""
    return " ".join(filter(None, [row.get("title") or "", row.get("description") or "",
                                  row.get("label") or ""]))


async def scenic_rules(db: Database, scenic_id: str) -> ContentFilter:
    """景区现在启用的主关键字 + 附关键字（命中任一就留）、过滤关键字（命中就丢）。"""
    main = [r["keyword"] for r in await db.fetch_all(
        f"SELECT keyword FROM `{T.SCENIC_KEYWORD}` WHERE scenic_id = %s AND enabled = 1 "
        f"ORDER BY sort_order, id", [scenic_id])]
    words = await db.fetch_all(
        f"SELECT kind, word FROM `{T.SCENIC_FILTER_WORD}` WHERE scenic_id = %s AND enabled = 1 "
        f"AND kind IN ('aux', 'exclude') ORDER BY sort_order, id", [scenic_id])
    keep = list(dict.fromkeys(main + [w["word"] for w in words if w["kind"] == "aux"]))
    return ContentFilter(enabled=True, mode=MODE_KEYWORD_PLUS, extra_keywords=keep,
                         exclude_keywords=[w["word"] for w in words if w["kind"] == "exclude"])


def judge(rules: ContentFilter, row: Dict[str, Any]) -> str:
    """不该留返回原因，该留返回空串。"""
    text = work_text(row)
    if rules.extra_keywords and not rules.matches(text, ""):
        return "不含关键字"
    hit = rules.blocked_by(text)
    return f"命中过滤词「{hit}」" if hit else ""


async def refilter_scenic(db: Database, scenic_id: str, *, apply: bool = False,
                          include_homepage: bool = False, remember: bool = False,
                          progress=None) -> Dict[str, Any]:
    """返回 {scenic_id, rules, scanned, kept, skipped_homepage, delete: [...], deleted_works, deleted_comments}。

    apply=False 只算不删（预览）。remember=True 时删掉的记进「已删除作品」，之后采集再搜到直接跳过
    （默认不记：改了关键字之后它们在采集时本来就过不了关，不用再记；记了反而以后改回词表也采不回来）。
    """
    rules = await scenic_rules(db, scenic_id)
    channels = list(CHANNELS_WITH_WORKS)
    placeholders = ", ".join(["%s"] * len(channels))
    report: Dict[str, Any] = {
        "scenic_id": scenic_id, "keep_words": rules.extra_keywords,
        "exclude_words": rules.exclude_keywords, "scanned": 0, "kept": 0,
        "skipped_homepage": 0, "delete": [], "deleted_works": 0, "deleted_comments": 0,
    }
    if not rules.extra_keywords and not rules.exclude_keywords:
        # 一个词都没配：判不了相关不相关，什么都别删
        report["note"] = "这个景区没有启用的主关键字 / 附关键字 / 过滤关键字，跳过"
        return report

    last_id = 0
    while True:
        rows = await db.fetch_all(
            f"SELECT id, channel, work_id, work_uk, title, description, label, source_keyword, hit_keywords "
            f"FROM `{T.WORKS}` WHERE scenic_id = %s AND channel IN ({placeholders}) AND id > %s "
            f"ORDER BY id ASC LIMIT %s", [scenic_id, *channels, last_id, BATCH])
        if not rows:
            break
        last_id = rows[-1]["id"]
        doomed: List[Dict[str, Any]] = []
        for row in rows:
            report["scanned"] += 1
            if not include_homepage and not (row.get("source_keyword") or row.get("hit_keywords")):
                report["skipped_homepage"] += 1
                continue
            reason = judge(rules, row)
            if not reason:
                report["kept"] += 1
                continue
            doomed.append({**row, "reason": reason})
        if doomed:
            counts = await _comment_counts(db, scenic_id, doomed)
            for row in doomed:
                row["comments"] = counts.get((row["channel"], row["work_id"]), 0)
                report["delete"].append({k: row[k] for k in
                                         ("id", "channel", "work_id", "title", "reason", "comments")})
            if apply:
                works, comments = await _delete(db, scenic_id, doomed, remember=remember)
                report["deleted_works"] += works
                report["deleted_comments"] += comments
        if progress is not None:
            progress(report)
        if len(rows) < BATCH:
            break
    return report


async def _comment_counts(db: Database, scenic_id: str, rows: List[Dict[str, Any]]) -> Dict[tuple, int]:
    out: Dict[tuple, int] = {}
    for channel in {r["channel"] for r in rows}:
        ids = [r["work_id"] for r in rows if r["channel"] == channel]
        for found in await db.fetch_all(
                f"SELECT work_id, COUNT(*) AS c FROM `{T.COMMENTS}` WHERE scenic_id = %s AND channel = %s "
                f"AND work_id IN ({', '.join(['%s'] * len(ids))}) GROUP BY work_id",
                [scenic_id, channel, *ids]):
            out[(channel, found["work_id"])] = int(found["c"])
    return out


async def _delete(db: Database, scenic_id: str, rows: List[Dict[str, Any]], *,
                  remember: bool) -> "tuple[int, int]":
    works = comments = 0
    async with db.transaction() as cur:
        for channel in {r["channel"] for r in rows}:
            part = [r for r in rows if r["channel"] == channel]
            ids = [r["work_id"] for r in part]
            marks = ", ".join(["%s"] * len(ids))
            comments += await cur.execute(
                f"DELETE FROM `{T.COMMENTS}` WHERE scenic_id = %s AND channel = %s AND work_id IN ({marks})",
                [scenic_id, channel, *ids])
            if remember:
                await cur.executemany(
                    f"INSERT INTO `{T.DELETED_WORK}` (work_uk, scenic_id, channel, work_id, title, comment_cnt) "
                    f"VALUES (%s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE deleted_at = NOW()",
                    [[r["work_uk"], scenic_id, channel, r["work_id"], (r.get("title") or "")[:600],
                      int(r.get("comments") or 0)] for r in part])
        ids = [r["id"] for r in rows]
        works += await cur.execute(
            f"DELETE FROM `{T.WORKS}` WHERE id IN ({', '.join(['%s'] * len(ids))})", ids)
    return works, int(comments or 0)
