"""作品/评论/创作者的批量写入与数据中心查询。

写入策略：
  - 按 work_uk / comment_uk 唯一键做 upsert，重复采集不会产生重复行
  - 写前先查一次已存在的 uk 集合，用来准确统计"新增"与"更新"
    （MySQL 的 affected_rows 在 executemany 下无法区分这两者）
  - 分批提交，单批默认 500 条，避免超过 max_allowed_packet
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..collectors.base import AuthorItem, CommentItem, WorkItem
from ..core.db import Database
from ..core.constants import ALL_CHANNELS
from ..core.logging import get_logger
from ..db import tables

logger = get_logger(__name__)

BATCH_SIZE = 500

WORK_COLUMNS = [
    "scenic_id", "scenic_name", "channel", "work_id", "work_url",
    "author_id", "author_name", "title", "description", "label",
    "image_list", "video_list", "likes", "collection_cnt", "comment_cnt",
    "shares", "location", "publish_time", "crawl_time",
    "extra_content", "source_keyword", "task_id",
]
# 重复命中时要刷新的列：计数类和内容类会变，创建来源不覆盖
WORK_UPDATE_COLUMNS = [
    "scenic_name", "work_url", "author_id", "author_name", "title", "description",
    "label", "image_list", "video_list", "likes", "collection_cnt", "comment_cnt",
    "shares", "location", "publish_time", "crawl_time", "extra_content",
]

COMMENT_COLUMNS = [
    "scenic_id", "scenic_name", "channel", "work_id", "comment_level",
    "comment_parent_id", "comment_id", "commenter_id", "image_list", "video_list",
    "location", "content", "likes", "extra_content", "publish_time", "crawl_time",
    "commenter_name", "root_comment_id", "sub_comment_count", "task_id",
]
COMMENT_UPDATE_COLUMNS = [
    "scenic_name", "comment_level", "comment_parent_id", "commenter_id",
    "image_list", "video_list", "location", "content", "likes", "extra_content",
    "publish_time", "crawl_time", "commenter_name", "root_comment_id",
    "sub_comment_count",
]

AUTHOR_COLUMNS = [
    "channel", "author_id", "author_name", "avatar", "signature", "gender",
    "location", "home_url", "fans_count", "follow_count", "works_count",
    "liked_count", "extra_content", "crawl_time",
]
AUTHOR_UPDATE_COLUMNS = [c for c in AUTHOR_COLUMNS if c not in ("channel", "author_id")]

_SEP = "\x1f"  # 与 schema.sql 里生成列用的 0x1F 保持一致


def work_uk(scenic_id: str, channel: str, work_id: str) -> str:
    return hashlib.md5(f"{scenic_id}{_SEP}{channel}{_SEP}{work_id}".encode("utf-8")).hexdigest()


def comment_uk(scenic_id: str, channel: str, work_id: str, comment_id: str) -> str:
    raw = f"{scenic_id}{_SEP}{channel}{_SEP}{work_id}{_SEP}{comment_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def author_uk(channel: str, author_id: str) -> str:
    return hashlib.md5(f"{channel}{_SEP}{author_id}".encode("utf-8")).hexdigest()


def _upsert_sql(table: str, columns: Sequence[str], update_columns: Sequence[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    column_list = ", ".join(f"`{c}`" for c in columns)
    updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in update_columns)
    return (
        f"INSERT INTO `{table}` ({column_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


#: 「合成作品」= 携程/同程为每个 POI 目标造的那一条壳作品，
#: 用来把景区点评挂在它下面（见 ctrip.py / tongcheng.py 的 _synthetic_work）。
#:
#: ⚠️ 判定方式从"翻 extra_content 里的 JSON"改成了"看是哪个平台"。
#: 这两个平台**只会**产出合成作品，没有别的来源；而反过来，
#: 其它平台永远不会产出合成作品。所以这个等价关系是成立的，
#: 而代价差得很远：原来那种写法是对 LONGTEXT 做 `LIKE '%...%'`，
#: 前导通配用不上任何索引，每一行都要把几十 KB 的原始 JSON 读出来比对——
#: 列表的 COUNT 和 SELECT 还各扫一遍。
SYNTHETIC_CHANNELS = ("ctrip", "tongcheng")

#: 数据页列表要用到的列。**不要写 SELECT ***。
#:
#: 这张表里 extra_content 是 LONGTEXT，存的是平台返回的整包原始 JSON，
#: 一条抖音作品能有几十 KB。而页面对它只用了一件事：判断这条是不是
#: 合成作品（一个布尔）。SELECT * 的代价是：数据库传输、Python 构造 dict、
#: FastAPI 序列化 JSON、浏览器解析，每一环都按这个体积走一遍——
#: 实测一页 20 条就有 409 KB，改完是 15 KB。
#: label 同理：只有服务端的关键字匹配用得上，页面上不显示。
WORK_LIST_COLUMNS = (
    "id, scenic_id, scenic_name, channel, work_id, work_url, "
    "author_id, author_name, title, description, "
    "image_list, video_list, likes, collection_cnt, comment_cnt, shares, "
    "location, publish_time, crawl_time, source_keyword, task_id, "
    "(channel IN ('ctrip', 'tongcheng')) AS is_synthetic"
)


class DataRepository:
    def __init__(self, db: Database):
        self.db = db
        #: (取数时刻, 结果)，见 scenic_overview
        self._overview_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None

    # ---------------- 写入 ----------------
    async def save_works(self, items: Sequence[WorkItem]) -> Tuple[int, int]:
        """返回 (新增数, 更新数)。"""
        if not items:
            return 0, 0
        sql = _upsert_sql(tables.WORKS, WORK_COLUMNS, WORK_UPDATE_COLUMNS)
        total_new = total_updated = 0

        for chunk in _chunks(items, BATCH_SIZE):
            keys = [work_uk(i.scenic_id, i.channel, i.work_id) for i in chunk]
            existing = await self._existing_keys(tables.WORKS, "work_uk", keys)
            rows = [[getattr(item, column) for column in WORK_COLUMNS] for item in chunk]
            await self.db.execute_many(sql, rows)
            new_count = sum(1 for key in keys if key not in existing)
            total_new += new_count
            total_updated += len(chunk) - new_count

        return total_new, total_updated

    async def save_comments(
        self, items: Sequence[CommentItem], *, skip_existing: bool = True
    ) -> Tuple[int, int]:
        """写评论。默认**跳过库里已有的**，只写新增。

        ⚠️ 为什么默认跳过：评论的正文一旦发出来就不会再变，会变的只有点赞数。
        而重复采集（同一个景区每天跑一轮）时，绝大多数评论都是上一轮已经收过的——
        全量 upsert 等于每天把几万行原样重写一遍，写放大非常可观，
        换来的只是老评论的点赞数。

        需要点赞数也跟着刷新时传 skip_existing=False
        （系统设置里的 `crawl.refresh_existing_comments`）。
        """
        if not items:
            return 0, 0
        sql = _upsert_sql(tables.COMMENTS, COMMENT_COLUMNS, COMMENT_UPDATE_COLUMNS)
        total_new = total_updated = 0

        for chunk in _chunks(items, BATCH_SIZE):
            keys = [
                comment_uk(i.scenic_id, i.channel, i.work_id, i.comment_id) for i in chunk
            ]
            existing = await self._existing_keys(tables.COMMENTS, "comment_uk", keys)
            if skip_existing:
                pairs = [(key, item) for key, item in zip(keys, chunk) if key not in existing]
                total_updated += len(chunk) - len(pairs)
                if not pairs:
                    continue
                chunk = [item for _, item in pairs]
            else:
                total_updated += sum(1 for key in keys if key in existing)
            rows = [[getattr(item, column) for column in COMMENT_COLUMNS] for item in chunk]
            await self.db.execute_many(sql, rows)
            total_new += sum(1 for key in keys if key not in existing)

        return total_new, total_updated

    async def save_authors(self, items: Sequence[AuthorItem]) -> int:
        if not items:
            return 0
        sql = _upsert_sql(tables.AUTHORS, AUTHOR_COLUMNS, AUTHOR_UPDATE_COLUMNS)
        rows = [[getattr(item, column) for column in AUTHOR_COLUMNS] for item in items]
        await self.db.execute_many(sql, rows)
        return len(rows)

    async def work_ids_collected_since(
        self, scenic_id: str, channel: str, since: datetime
    ) -> set:
        """这个景区+平台下，since 之后已经采过的作品 ID。

        用来在**重跑**时跳过今天已经采成功的作品：
        关键字失败重试、或者同一天里任务被再跑一次，
        原来都会把每条作品重新点开、评论重新翻一遍——
        拟人模式下那是每条几十秒的真实点击，几百条就是几个小时。

        ⚠️ **必须真的采到过评论才算数**。

        原来这里只查作品表：只要今天写过这条作品的行，就算"采过了"。
        可作品行是**先写的**——评论翻不翻得到都会写。于是一条作品因为
        采集器出问题一条评论都没拿到，照样被记成"今天采过"，
        当天再怎么重跑都会跳过它，评论就**永远补不回来**。

        实跑现场（2026-09-05 八大处公园）：前几轮因为点错卡片，
        27 条作品的评论全是 0，之后每次重跑第一个关键字都是
        「今天已经采过它的评论，本轮不再翻」——用户看到的是
        "第一个关键字直接就没采"。

        所以判据改成"这条作品今天**有评论落库**"。
        代价是"确实没有评论的作品"每轮会被重新翻一遍；
        比起评论永久丢失，这个代价可以接受。
        """
        rows = await self.db.fetch_all(
            "SELECT DISTINCT w.work_id "
            "FROM `src_opinion_social_work_di` AS w "
            "JOIN `src_opinion_social_work_comment_di` AS c "
            "  ON c.work_id = w.work_id AND c.channel = w.channel "
            "WHERE w.scenic_id = %s AND w.channel = %s "
            "  AND w.crawl_time >= %s AND c.crawl_time >= %s",
            [scenic_id, channel, since, since],
        )
        return {row["work_id"] for row in rows}

    async def _existing_keys(self, table: str, key_column: str, keys: Sequence[str]) -> set:
        if not keys:
            return set()
        placeholders = ", ".join(["%s"] * len(keys))
        rows = await self.db.fetch_all(
            f"SELECT `{key_column}` FROM `{table}` WHERE `{key_column}` IN ({placeholders})",
            list(keys),
        )
        return {row[key_column] for row in rows}

    # ---------------- 数据中心查询 ----------------
    #: 概览缓存的有效期（秒）。数据中心页面会周期性重拉，
    #: 而这个查询要把两张大表整个聚合一遍——每次都真算太贵。
    OVERVIEW_TTL_SECONDS = 30
    #: 采集刚写完一批之后，这份缓存还能再用多久（秒）。见 invalidate_overview_cache。
    FRESH_AFTER_WRITE_SECONDS = 5

    async def scenic_overview(self, *, use_cache: bool = True) -> List[Dict[str, Any]]:
        """景区 × 平台 的作品/评论数量概览，数据中心首屏用。

        ⚠️ 这是全系统**最贵**的一个查询：它要把作品表和评论表各整个聚合一遍。
        评论表上百万行时，一次要几百毫秒到几秒，而数据中心页面会反复拉它。
        两个措施：
          1. 平台清单用常量，不再 `SELECT DISTINCT channel` ——
             那两个子查询每次都是两次全表扫描，而平台就那六个，是已知的。
          2. 结果缓存 30 秒。采集是持续在跑的，数字本来就时刻在变，
             晚 30 秒看到和实时看到对用户没有任何区别。
        """
        now = time.monotonic()
        if use_cache and self._overview_cache is not None:
            cached_at, rows = self._overview_cache
            if now - cached_at < self.OVERVIEW_TTL_SECONDS:
                return rows

        # 平台是固定的六个，拼成 SELECT 'douyin' UNION ALL ... 当作常量表用
        channel_rows = " UNION ALL ".join(
            f"SELECT %s AS channel" for _ in ALL_CHANNELS
        )
        rows = await self.db.fetch_all(
            f"""
            SELECT s.scenic_id, s.scenic_name, t.channel,
                   COALESCE(w.work_cnt, 0)    AS work_cnt,
                   COALESCE(c.comment_cnt, 0) AS comment_cnt,
                   GREATEST(COALESCE(w.last_crawl, '1970-01-01'),
                            COALESCE(c.last_crawl, '1970-01-01')) AS last_crawl
            FROM `{tables.SCENIC}` s
            CROSS JOIN ({channel_rows}) t
            LEFT JOIN (
                SELECT scenic_id, channel, COUNT(*) AS work_cnt, MAX(crawl_time) AS last_crawl
                FROM `{tables.WORKS}` GROUP BY scenic_id, channel
            ) w ON w.scenic_id = s.scenic_id AND w.channel = t.channel
            LEFT JOIN (
                SELECT scenic_id, channel, COUNT(*) AS comment_cnt, MAX(crawl_time) AS last_crawl
                FROM `{tables.COMMENTS}` GROUP BY scenic_id, channel
            ) c ON c.scenic_id = s.scenic_id AND c.channel = t.channel
            WHERE COALESCE(w.work_cnt, 0) > 0 OR COALESCE(c.comment_cnt, 0) > 0
            ORDER BY s.scenic_id, t.channel
            """,
            list(ALL_CHANNELS),
        )
        self._overview_cache = (now, rows)
        return rows

    def invalidate_overview_cache(self) -> None:
        """采集写完一批数据了。

        ⚠️ 这里**故意不清空缓存**，只是把它的有效期缩短到几秒。

        为什么：清空是采集器每 flush 一批（200 条）就调一次的，
        而采集在这个系统里是常态——于是 30 秒的缓存命中率接近 0，
        首页和数据页每次打开都要真跑一遍"两张大表各全量 GROUP BY"，
        代码注释自己都写了那要几百毫秒到几秒。
        缓存等于形同虚设，正好在最忙的时候失效。

        而上面的注释也已经论证过：数字晚几十秒对用户没有任何区别。
        所以折中成"采集期间把 TTL 压到 5 秒"——正在采的时候数字仍然
        跟得上，但连续 flush 不会把缓存打穿。
        """
        if self._overview_cache is None:
            return
        cached_at, rows = self._overview_cache
        # 把时间戳往前挪，等效于"这份缓存再活 FRESH_AFTER_WRITE_SECONDS 秒"
        deadline = time.time() - (self.OVERVIEW_TTL_SECONDS - self.FRESH_AFTER_WRITE_SECONDS)
        self._overview_cache = (min(cached_at, deadline), rows)

    async def list_works(
        self,
        *,
        scenic_id: Optional[str] = None,
        channel: Optional[str] = None,
        keyword: Optional[str] = None,
        author_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        include_synthetic: bool = True,
        page: int = 1,
        page_size: int = 20,
        order_by: str = "publish_time",
        order: str = "desc",
    ) -> Dict[str, Any]:
        where, args = self._works_filters(
            scenic_id, channel, keyword, author_id, start_time, end_time, include_synthetic
        )
        allowed_order = {"publish_time", "crawl_time", "likes", "comment_cnt", "id"}
        order_column = order_by if order_by in allowed_order else "publish_time"
        direction = "ASC" if str(order).lower() == "asc" else "DESC"

        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `src_opinion_social_work_di` {where}", args, 0
        )
        offset = max(0, (page - 1) * page_size)
        rows = await self.db.fetch_all(
            f"SELECT {WORK_LIST_COLUMNS} FROM `src_opinion_social_work_di` {where} "
            f"ORDER BY `{order_column}` {direction}, id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), int(offset)],
        )
        for row in rows:
            # SQL 里算好的 0/1 转成布尔，前端不用再解析 JSON
            row["is_synthetic"] = bool(row.get("is_synthetic"))
        return {"total": int(total or 0), "page": page, "page_size": page_size, "items": rows}

    @staticmethod
    def _works_filters(
        scenic_id, channel, keyword, author_id, start_time, end_time, include_synthetic
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        args: List[Any] = []
        if scenic_id:
            clauses.append("scenic_id = %s")
            args.append(scenic_id)
        if channel:
            clauses.append("channel = %s")
            args.append(channel)
        if keyword:
            clauses.append("(title LIKE %s OR description LIKE %s OR source_keyword = %s)")
            args.extend([f"%{keyword}%", f"%{keyword}%", keyword])
        if author_id:
            clauses.append("author_id = %s")
            args.append(author_id)
        if start_time:
            clauses.append("publish_time >= %s")
            args.append(start_time)
        if end_time:
            clauses.append("publish_time <= %s")
            args.append(end_time)
        if not include_synthetic:
            # 携程/同程的合成作品行。见 SYNTHETIC_CHANNELS 的说明：
            # 这里原来是对 LONGTEXT 做前导通配的 NOT LIKE，是全表最贵的一个条件。
            placeholders = ", ".join(["%s"] * len(SYNTHETIC_CHANNELS))
            clauses.append(f"channel NOT IN ({placeholders})")
            args.extend(SYNTHETIC_CHANNELS)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", args

    async def list_comments(
        self,
        *,
        channel: str,
        work_id: str,
        scenic_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        level: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """按层级取评论。

        parent_id 为 None 时取一级评论；传了 parent_id 就取它的直接子评论——
        前端「点击展开」就是拿着上一级的 comment_id 再调一次本接口。
        """
        clauses = ["channel = %s", "work_id = %s"]
        args: List[Any] = [channel, work_id]
        if scenic_id:
            clauses.append("scenic_id = %s")
            args.append(scenic_id)
        if parent_id is not None:
            clauses.append("comment_parent_id = %s")
            args.append(parent_id)
        elif level:
            clauses.append("comment_level = %s")
            args.append(level)
        else:
            clauses.append("comment_level = 'level_1'")

        where = "WHERE " + " AND ".join(clauses)
        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `src_opinion_social_work_comment_di` {where}", args, 0
        )
        offset = max(0, (page - 1) * page_size)
        rows = await self.db.fetch_all(
            f"SELECT * FROM `src_opinion_social_work_comment_di` {where} "
            f"ORDER BY publish_time DESC, id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), int(offset)],
        )
        return {"total": int(total or 0), "page": page, "page_size": page_size, "items": rows}

    async def comment_thread(self, channel: str, work_id: str, root_comment_id: str) -> List[Dict]:
        """一次取出整条会话（一级 + 其下所有层级），用于一键展开。"""
        return await self.db.fetch_all(
            "SELECT * FROM `src_opinion_social_work_comment_di` "
            "WHERE channel = %s AND work_id = %s AND root_comment_id = %s "
            "ORDER BY comment_level ASC, publish_time ASC",
            [channel, work_id, root_comment_id],
        )

    async def list_authors(
        self, *, channel: Optional[str] = None, scenic_id: Optional[str] = None,
        page: int = 1, page_size: int = 20,
    ) -> Dict[str, Any]:
        """创作者列表，按该景区下的作品数排序。"""
        clauses: List[str] = []
        args: List[Any] = []
        join = ""
        if scenic_id:
            join = (
                "INNER JOIN (SELECT DISTINCT channel, author_id FROM `src_opinion_social_work_di` "
                "WHERE scenic_id = %s) w ON w.channel = a.channel AND w.author_id = a.author_id"
            )
            args.append(scenic_id)
        if channel:
            clauses.append("a.channel = %s")
            args.append(channel)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `src_opinion_social_authors` a {join} {where}", args, 0
        )
        offset = max(0, (page - 1) * page_size)
        rows = await self.db.fetch_all(
            f"SELECT a.* FROM `src_opinion_social_authors` a {join} {where} "
            f"ORDER BY a.fans_count DESC, a.id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), int(offset)],
        )
        return {"total": int(total or 0), "page": page, "page_size": page_size, "items": rows}

    # ---------------- 导出 ----------------
    async def iter_export_rows(
        self, table: str, filters: Dict[str, Any], chunk_size: int = 5000
    ):
        """按主键游标分批取，避免大 OFFSET 越翻越慢、也不会把结果全读进内存。"""
        if table not in tables.EXPORTABLE:
            raise ValueError(f"不支持导出的表：{table}")

        clauses: List[str] = []
        args: List[Any] = []
        for column, value in filters.items():
            if value in (None, ""):
                continue
            if column == "start_time":
                clauses.append("publish_time >= %s")
                args.append(value)
            elif column == "end_time":
                clauses.append("publish_time <= %s")
                args.append(value)
            elif column in ("scenic_id", "channel", "task_id", "work_id"):
                clauses.append(f"`{column}` = %s")
                args.append(value)

        last_id = 0
        while True:
            all_clauses = [*clauses, "id > %s"]
            where = "WHERE " + " AND ".join(all_clauses)
            rows = await self.db.fetch_all(
                f"SELECT * FROM `{table}` {where} ORDER BY id ASC LIMIT %s",
                [*args, last_id, int(chunk_size)],
            )
            if not rows:
                return
            for row in rows:
                yield row
            last_id = rows[-1]["id"]
            if len(rows) < chunk_size:
                return


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
