"""景区档案的读写。三个渠道共用一张表，靠 (channel, poi_id) 区分。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import tables
from ..utils import normalize as nz

#: upsert 时**不覆盖**的列。
#:
#: scenic_id：只是重采档案（没带关联）时，不能把已经建立的景区关联清掉。
#: 用户可能几个月前就把这条档案建成景区了，一次例行重采就断链，
#: 而页面上完全看不出发生过什么。
_KEEP_ON_CONFLICT = {"scenic_id"}

#: 列表页采到的列。详情单独更新——把它们一起 VALUES 进去，
#: 会用列表页的空值把上一轮采到的详情**冲掉**。
LIST_COLUMNS = [
    "poi_name", "province", "city_id", "city_name",
    "address", "scenic_level", "source_url", "region_id",
]

DETAIL_COLUMNS = ["open_time", "tel", "scenic_intro", "discount_policy", "amenity"]


class ArchiveStore:
    def __init__(self, db):
        self.db = db

    # ---------------- 区域清单 ----------------
    async def save_regions(self, channel: str, regions: List[Dict[str, Any]]) -> int:
        if not regions:
            return 0
        sql = (
            f"INSERT INTO `{tables.ARCHIVE_REGION}` "
            f"(channel, region_id, region_name, level) VALUES (%s,%s,%s,%s) "
            f"ON DUPLICATE KEY UPDATE region_name = VALUES(region_name), "
            f"level = VALUES(level)"
        )
        args = [[channel,
                 nz.truncate(nz.to_text(r.get("region_id")), 100),
                 nz.truncate(nz.to_text(r.get("region_name")), 100),
                 nz.truncate(nz.to_text(r.get("level")) or "province", 20)]
                for r in regions if nz.to_text(r.get("region_id"))]
        if not args:
            return 0
        await self.db.execute_many(sql, args)
        return len(args)

    async def list_regions(self, channel: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            f"SELECT region_id, region_name, level, poi_count, last_sync_time "
            f"FROM `{tables.ARCHIVE_REGION}` WHERE channel = %s "
            f"ORDER BY level DESC, region_name ASC", [channel])

    async def delete_region(self, channel: str, region_id: str) -> None:
        await self.db.execute(
            f"DELETE FROM `{tables.ARCHIVE_REGION}` "
            f"WHERE channel = %s AND region_id = %s", [channel, region_id])

    async def mark_region_done(self, channel: str, region_id: str, count: int) -> None:
        await self.db.execute(
            f"UPDATE `{tables.ARCHIVE_REGION}` SET poi_count = %s, last_sync_time = %s "
            f"WHERE channel = %s AND region_id = %s",
            [int(count), datetime.now(), channel, region_id])

    # ---------------- 档案 ----------------
    async def upsert_many(self, channel: str, rows: List[Dict[str, Any]],
                          *, supports_detail: bool) -> Dict[str, int]:
        """写一批档案。**存在的更新，不存在的插入**，永远不会写重。

        唯一键是 (channel, poi_id)，所以同一个景区反复采只会更新那一行。
        返回受影响行数——MySQL 对 upsert 的"更新"计 2、"插入"计 1，
        所以这里另外查一次真实的新增数，不用那个会骗人的数字。
        """
        if not rows:
            return {"total": 0, "created": 0, "updated": 0}

        ids = [nz.to_text(r.get("poi_id")) for r in rows if nz.to_text(r.get("poi_id"))]
        existed = await self._existing_ids(channel, ids)

        now = datetime.now()
        # 不支持详情的渠道直接标 unsupported，免得一屏"待采集"让人反复重试
        status = "pending" if supports_detail else "unsupported"
        cols = ["channel", "poi_id", "crawl_time", "detail_status"] + LIST_COLUMNS
        updates = ", ".join(
            f"`{c}` = VALUES(`{c}`)" for c in cols
            if c not in ("channel", "poi_id") and c not in _KEEP_ON_CONFLICT
            # detail_status 只在**新行**上取 VALUES；老行已经采过详情的
            # 不能被列表重采打回 pending，否则每次重采都要把详情重跑一遍。
            and c != "detail_status"
        )
        updates += (", `detail_status` = IF(`detail_status` = 'success', "
                    "'success', VALUES(`detail_status`))")
        sql = (f"INSERT INTO `{tables.SCENIC_POI_INFO}` "
               f"({', '.join('`' + c + '`' for c in cols)}) "
               f"VALUES ({', '.join(['%s'] * len(cols))}) "
               f"ON DUPLICATE KEY UPDATE {updates}")

        args = []
        for row in rows:
            poi_id = nz.to_text(row.get("poi_id"))
            if not poi_id:
                continue
            args.append([channel, poi_id, now, status] + [
                self._clean(c, row.get(c)) for c in LIST_COLUMNS
            ])
        if args:
            await self.db.execute_many(sql, args)

        created = len([i for i in ids if i not in existed])
        return {"total": len(args), "created": created,
                "updated": len(args) - created}

    @staticmethod
    def _clean(column: str, value: Any) -> str:
        limits = {"poi_name": 200, "province": 50, "city_id": 100,
                  "city_name": 100, "address": 500, "scenic_level": 50,
                  "source_url": 500, "region_id": 100}
        text = nz.to_text(value)
        cap = limits.get(column)
        return nz.truncate(text, cap) if cap else text

    async def _existing_ids(self, channel: str, ids: List[str]) -> set:
        found: set = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ", ".join(["%s"] * len(chunk))
            rows = await self.db.fetch_all(
                f"SELECT poi_id FROM `{tables.SCENIC_POI_INFO}` "
                f"WHERE channel = %s AND poi_id IN ({marks})",
                [channel, *chunk])
            found.update(r["poi_id"] for r in rows)
        return found

    async def save_detail(self, channel: str, poi_id: str,
                          detail: Dict[str, str]) -> None:
        sets = ", ".join(f"`{c}` = %s" for c in DETAIL_COLUMNS)
        await self.db.execute(
            f"UPDATE `{tables.SCENIC_POI_INFO}` SET {sets}, "
            f"detail_status = 'success', detail_error = '', detail_time = %s "
            f"WHERE channel = %s AND poi_id = %s",
            [*[nz.to_text(detail.get(c)) for c in DETAIL_COLUMNS],
             datetime.now(), channel, poi_id])

    async def mark_detail_error(self, channel: str, poi_id: str, error: str) -> None:
        await self.db.execute(
            f"UPDATE `{tables.SCENIC_POI_INFO}` SET detail_status = 'error', "
            f"detail_error = %s WHERE channel = %s AND poi_id = %s",
            [nz.truncate(nz.to_text(error), 500), channel, poi_id])

    async def pending_detail_ids(self, channel: str, region_id: str = "",
                                 limit: int = 500) -> List[str]:
        clauses = ["channel = %s", "detail_status IN ('pending', 'error')"]
        args: List[Any] = [channel]
        if region_id:
            clauses.append("region_id = %s")
            args.append(region_id)
        rows = await self.db.fetch_all(
            f"SELECT poi_id FROM `{tables.SCENIC_POI_INFO}` "
            f"WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT %s",
            [*args, int(limit)])
        return [r["poi_id"] for r in rows]

    async def get(self, channel: str, poi_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM `{tables.SCENIC_POI_INFO}` "
            f"WHERE channel = %s AND poi_id = %s", [channel, poi_id])

    async def link_scenic(self, channel: str, poi_id: str, scenic_id: str) -> None:
        await self.db.execute(
            f"UPDATE `{tables.SCENIC_POI_INFO}` SET scenic_id = %s "
            f"WHERE channel = %s AND poi_id = %s", [scenic_id, channel, poi_id])

    async def search(self, *, channel: str = "", province: str = "",
                     city: str = "", keyword: str = "", linked: str = "",
                     level: str = "", scenic_id: str = "", page: int = 1,
                     page_size: int = 20) -> Dict[str, Any]:
        clauses: List[str] = ["1 = 1"]
        args: List[Any] = []
        if channel:
            clauses.append("channel = %s"); args.append(channel)
        if scenic_id:
            # 景区详情页的「景区档案」标签页用：这个景区在几个平台上各有一条
            clauses.append("scenic_id = %s"); args.append(scenic_id)
        if province:
            clauses.append("province = %s"); args.append(province)
        if city:
            clauses.append("city_name LIKE %s"); args.append(f"%{city}%")
        if level:
            clauses.append("scenic_level LIKE %s"); args.append(f"%{level}%")
        if keyword:
            clauses.append("(poi_name LIKE %s OR address LIKE %s OR poi_id = %s)")
            args += [f"%{keyword}%", f"%{keyword}%", keyword]
        if linked == "yes":
            clauses.append("scenic_id <> ''")
        elif linked == "no":
            clauses.append("scenic_id = ''")
        where = " AND ".join(clauses)

        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.SCENIC_POI_INFO}` WHERE {where}",
            args, 0)
        # 列表页不返回介绍/政策/设施三个大字段——它们是 LONGTEXT，
        # 一页 20 条能拉回几百 KB，而列表上一个字都不显示。
        rows = await self.db.fetch_all(
            f"SELECT id, channel, poi_id, scenic_id, poi_name, province, city_id, "
            f"city_name, address, scenic_level, tel, open_time, source_url, "
            f"detail_status, detail_error, detail_time, crawl_time, region_id "
            f"FROM `{tables.SCENIC_POI_INFO}` WHERE {where} "
            f"ORDER BY province ASC, city_name ASC, poi_name ASC "
            f"LIMIT %s OFFSET %s",
            [*args, int(page_size), max(0, (page - 1) * page_size)])
        return {"total": int(total or 0), "page": page,
                "page_size": page_size, "items": rows}

    async def provinces(self, channel: str = "") -> List[Dict[str, Any]]:
        clauses = ["province <> ''"]
        args: List[Any] = []
        if channel:
            clauses.append("channel = %s"); args.append(channel)
        return await self.db.fetch_all(
            f"SELECT province, COUNT(*) AS total FROM `{tables.SCENIC_POI_INFO}` "
            f"WHERE {' AND '.join(clauses)} GROUP BY province ORDER BY province",
            args)

    async def stats(self) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            f"SELECT channel, COUNT(*) AS total, "
            f"SUM(CASE WHEN scenic_id <> '' THEN 1 ELSE 0 END) AS linked, "
            f"SUM(CASE WHEN detail_status = 'success' THEN 1 ELSE 0 END) AS detailed, "
            f"MAX(crawl_time) AS last_crawl "
            f"FROM `{tables.SCENIC_POI_INFO}` GROUP BY channel")
