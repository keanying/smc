"""景区、景区关键字、景区平台目标（POI/主页）的读写。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..core import content_filter
from ..core.db import Database
from ..db import tables


class ScenicRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------------- 景区 ----------------
    async def list_scenics(
        self, *, keyword: str = "", enabled_only: bool = False,
        page: int = 1, page_size: int = 50,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        args: List[Any] = []
        if keyword:
            clauses.append("(scenic_id LIKE %s OR scenic_name LIKE %s)")
            args.extend([f"%{keyword}%", f"%{keyword}%"])
        if enabled_only:
            clauses.append("enabled = 1")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = await self.db.fetch_value(f"SELECT COUNT(*) AS c FROM `src_opinion_social_scenic` {where}", args, 0)
        rows = await self.db.fetch_all(
            f"SELECT * FROM `src_opinion_social_scenic` {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            [*args, int(page_size), max(0, (page - 1) * page_size)],
        )
        await self._attach_counts(rows)
        return {"total": int(total or 0), "page": page, "page_size": page_size, "items": rows}

    async def _attach_counts(self, rows: List[Dict[str, Any]]) -> None:
        """给每个景区补上「启用的关键字数 / 采集目标数」。

        ⚠️ 这里以前是在 for 循环里逐个 fetch_value，两条 COUNT 一行。
        看着人畜无害，但列表页是按 page_size=500 调的（下拉框要全量），
        于是一次请求变成 1 + 1 + 500×2 = 1002 次**串行**往返，
        每次还各自 acquire/release 一次连接。单条 0.5ms 也要半秒起，
        网络稍差就是好几秒——三个页面（数据、任务、新建任务）
        都卡在这一步才开始渲染。

        改成两条聚合查询，1002 → 4。
        """
        if not rows:
            return
        scenic_ids = [row["scenic_id"] for row in rows]
        placeholders = ", ".join(["%s"] * len(scenic_ids))
        keyword_counts = await self.db.fetch_all(
            f"SELECT scenic_id, COUNT(*) AS c FROM `src_opinion_scenic_keyword` "
            f"WHERE enabled = 1 AND scenic_id IN ({placeholders}) GROUP BY scenic_id",
            scenic_ids,
        )
        target_counts = await self.db.fetch_all(
            f"SELECT scenic_id, COUNT(*) AS c FROM `src_opinion_scenic_platform_target` "
            f"WHERE enabled = 1 AND scenic_id IN ({placeholders}) GROUP BY scenic_id",
            scenic_ids,
        )
        keyword_map = {r["scenic_id"]: int(r["c"]) for r in keyword_counts}
        target_map = {r["scenic_id"]: int(r["c"]) for r in target_counts}
        for row in rows:
            # 没有关键字的景区不会出现在 GROUP BY 结果里，补 0
            row["keyword_count"] = keyword_map.get(row["scenic_id"], 0)
            row["target_count"] = target_map.get(row["scenic_id"], 0)

    async def list_scenic_options(self, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        """下拉框用的精简景区列表：只有 id 和名字。

        数据页/任务页/新建任务页要的只是一个"选景区"的下拉框，
        却在调完整的 list_scenics(page_size=500)——连 remark、
        关键字数、目标数一起拉回来，再渲染成一个只显示名字的 select。
        这个接口把那三个页面的开销压到一条不带 COUNT 的查询。
        """
        where = "WHERE enabled = 1" if enabled_only else ""
        return await self.db.fetch_all(
            f"SELECT scenic_id, scenic_name FROM `src_opinion_social_scenic` "
            f"{where} ORDER BY id DESC",
        )

    async def get_scenic(self, scenic_id: str) -> Optional[Dict]:
        return await self.db.fetch_one("SELECT * FROM `src_opinion_social_scenic` WHERE scenic_id = %s", [scenic_id])

    async def upsert_scenic(self, data: Dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO `src_opinion_social_scenic` (scenic_id, scenic_name, province, city, remark, enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                scenic_name = VALUES(scenic_name), province = VALUES(province),
                city = VALUES(city), remark = VALUES(remark), enabled = VALUES(enabled)
            """,
            [
                data["scenic_id"], data["scenic_name"], data.get("province"),
                data.get("city"), data.get("remark"), int(data.get("enabled", 1)),
            ],
        )

    async def bulk_import_scenics(self, rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """批量导入景区（CSV/Excel 导入用），返回新增与更新数量。"""
        if not rows:
            return {"created": 0, "updated": 0}
        ids = [r["scenic_id"] for r in rows]
        placeholders = ", ".join(["%s"] * len(ids))
        existing_rows = await self.db.fetch_all(
            f"SELECT scenic_id FROM `src_opinion_social_scenic` WHERE scenic_id IN ({placeholders})", ids
        )
        existing = {r["scenic_id"] for r in existing_rows}
        await self.db.execute_many(
            """
            INSERT INTO `src_opinion_social_scenic` (scenic_id, scenic_name, province, city, remark, enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                scenic_name = VALUES(scenic_name), province = VALUES(province),
                city = VALUES(city), remark = VALUES(remark)
            """,
            [
                [r["scenic_id"], r["scenic_name"], r.get("province"), r.get("city"),
                 r.get("remark"), int(r.get("enabled", 1))]
                for r in rows
            ],
        )
        created = sum(1 for i in ids if i not in existing)
        return {"created": created, "updated": len(ids) - created}

    async def delete_scenic(self, scenic_id: str) -> None:
        """删除景区连同其关键字和目标；已采集的数据保留，避免误删业务数据。"""
        async with self.db.transaction() as cur:
            await cur.execute("DELETE FROM `src_opinion_scenic_keyword` WHERE scenic_id = %s", [scenic_id])
            await cur.execute("DELETE FROM `src_opinion_scenic_platform_target` WHERE scenic_id = %s", [scenic_id])
            await cur.execute("DELETE FROM `src_opinion_social_scenic` WHERE scenic_id = %s", [scenic_id])

    # ---------------- 关键字 ----------------
    async def list_keywords(
        self, scenic_id: str, *, enabled_only: bool = False, limit: int = 0
    ) -> List[Dict]:
        clauses = ["scenic_id = %s"]
        args: List[Any] = [scenic_id]
        if enabled_only:
            clauses.append("enabled = 1")
        sql = (
            f"SELECT * FROM `src_opinion_scenic_keyword` WHERE {' AND '.join(clauses)} "
            f"ORDER BY sort_order ASC, id ASC"
        )
        if limit and limit > 0:
            sql += " LIMIT %s"
            args.append(int(limit))
        return await self.db.fetch_all(sql, args)

    async def add_keywords(self, scenic_id: str, keywords: Sequence[str]) -> Dict[str, int]:
        cleaned = [k.strip() for k in keywords if k and k.strip()]
        # 去重但保序
        seen: set = set()
        unique = [k for k in cleaned if not (k in seen or seen.add(k))]
        if not unique:
            return {"added": 0, "skipped": 0}

        placeholders = ", ".join(["%s"] * len(unique))
        existing_rows = await self.db.fetch_all(
            f"SELECT keyword FROM `src_opinion_scenic_keyword` WHERE scenic_id = %s AND keyword IN ({placeholders})",
            [scenic_id, *unique],
        )
        existing = {r["keyword"] for r in existing_rows}
        start_order = await self.db.fetch_value(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM `src_opinion_scenic_keyword` WHERE scenic_id = %s",
            [scenic_id], 0,
        )
        await self.db.execute_many(
            "INSERT INTO `src_opinion_scenic_keyword` (scenic_id, keyword, sort_order) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE enabled = 1",
            [
                [scenic_id, keyword, int(start_order) + index + 1]
                for index, keyword in enumerate(unique)
            ],
        )
        added = sum(1 for k in unique if k not in existing)
        return {"added": added, "skipped": len(unique) - added}

    async def delete_keyword(self, keyword_id: int) -> None:
        await self.db.execute("DELETE FROM `src_opinion_scenic_keyword` WHERE id = %s", [keyword_id])

    async def set_keyword_enabled(self, keyword_id: int, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE `src_opinion_scenic_keyword` SET enabled = %s WHERE id = %s", [int(enabled), keyword_id]
        )

    # ---------------- 平台目标（POI / 主页） ----------------
    # ---------------- 附关键字 / 过滤关键字 ----------------
    #: 两类词的合法取值。aux=附关键字（命中就留存），exclude=过滤关键字（命中就丢弃）
    FILTER_KINDS = ("aux", "exclude")
    #: 每类各自的上限，和 core/content_filter.py 里的两个常量**逐一**对齐——
    #: 这边放进去、那边截断的话，用户会看到"配了 700 个，只有 200 个生效"，
    #: 而页面上又明明显示 700 个都在，极难对上。
    MAX_FILTER_WORDS: Dict[str, int] = {
        "aux": content_filter.MAX_AUX_KEYWORDS,        # 附关键字 700
        "exclude": content_filter.MAX_EXCLUDE_KEYWORDS,  # 过滤关键字 200
    }

    @classmethod
    def max_words_of(cls, kind: str) -> int:
        return cls.MAX_FILTER_WORDS.get(kind, content_filter.MAX_EXCLUDE_KEYWORDS)

    @staticmethod
    def _check_kind(kind: str) -> str:
        kind = (kind or "").strip().lower()
        if kind not in ScenicRepository.FILTER_KINDS:
            raise ValueError(
                f"词表类型只能是 aux（附关键字）或 exclude（过滤关键字），收到 {kind!r}")
        return kind

    async def list_filter_words(
        self, scenic_id: str, kind: str = "", *, enabled_only: bool = False
    ) -> List[Dict]:
        """列出景区的附关键字 / 过滤关键字。kind 留空就两类都返回。"""
        clauses = ["scenic_id = %s"]
        args: List[Any] = [scenic_id]
        if kind:
            clauses.append("kind = %s")
            args.append(self._check_kind(kind))
        if enabled_only:
            clauses.append("enabled = 1")
        return await self.db.fetch_all(
            f"SELECT * FROM `{tables.SCENIC_FILTER_WORD}` "
            f"WHERE {' AND '.join(clauses)} ORDER BY kind ASC, sort_order ASC, id ASC",
            args,
        )

    async def add_filter_words(
        self, scenic_id: str, kind: str, words: Sequence[str]
    ) -> Dict[str, int]:
        """批量加词。已存在的只把 enabled 置回 1，不报错。

        超出上限的部分**明确告诉用户被丢了几个**，而不是安静地截断——
        安静截断的现象是"我明明贴了 250 个词，怎么后面那些不生效"。

        两类的上限不一样：附关键字 700，过滤关键字 200。
        """
        kind = self._check_kind(kind)
        cleaned = [w.strip() for w in words if w and w.strip()]
        seen: set = set()
        unique = [w for w in cleaned if not (w in seen or seen.add(w))]
        if not unique:
            return {"added": 0, "skipped": 0, "dropped": 0}

        current = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.SCENIC_FILTER_WORD}` "
            f"WHERE scenic_id = %s AND kind = %s", [scenic_id, kind], 0)
        room = max(0, self.max_words_of(kind) - int(current or 0))
        dropped = max(0, len(unique) - room)
        unique = unique[:room]
        if not unique:
            return {"added": 0, "skipped": 0, "dropped": dropped}

        placeholders = ", ".join(["%s"] * len(unique))
        existing_rows = await self.db.fetch_all(
            f"SELECT word FROM `{tables.SCENIC_FILTER_WORD}` "
            f"WHERE scenic_id = %s AND kind = %s AND word IN ({placeholders})",
            [scenic_id, kind, *unique],
        )
        existing = {r["word"] for r in existing_rows}
        start_order = await self.db.fetch_value(
            f"SELECT COALESCE(MAX(sort_order), 0) AS m FROM `{tables.SCENIC_FILTER_WORD}` "
            f"WHERE scenic_id = %s AND kind = %s", [scenic_id, kind], 0)
        await self.db.execute_many(
            f"INSERT INTO `{tables.SCENIC_FILTER_WORD}` "
            f"(scenic_id, kind, word, sort_order) VALUES (%s, %s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE enabled = 1",
            [[scenic_id, kind, word, int(start_order) + i + 1]
             for i, word in enumerate(unique)],
        )
        added = sum(1 for w in unique if w not in existing)
        return {"added": added, "skipped": len(unique) - added, "dropped": dropped}

    async def delete_filter_word(self, word_id: int) -> None:
        await self.db.execute(
            f"DELETE FROM `{tables.SCENIC_FILTER_WORD}` WHERE id = %s", [word_id])

    async def set_filter_word_enabled(self, word_id: int, enabled: bool) -> None:
        await self.db.execute(
            f"UPDATE `{tables.SCENIC_FILTER_WORD}` SET enabled = %s WHERE id = %s",
            [1 if enabled else 0, word_id])

    async def filter_words_for(self, scenic_id: str) -> Dict[str, List[str]]:
        """采集时用的那份：只要启用的词，按类型分好。

        返回 {"aux": [...], "exclude": [...]}，两个键**一定存在**，
        省得调用方每次判空。
        """
        rows = await self.list_filter_words(scenic_id, enabled_only=True)
        out: Dict[str, List[str]] = {"aux": [], "exclude": []}
        for row in rows:
            bucket = out.get(row.get("kind"))
            if bucket is not None:
                bucket.append(row["word"])
        return out

    async def list_targets(
        self, scenic_id: str, *, channel: str = "", target_type: str = "",
        enabled_only: bool = False,
    ) -> List[Dict]:
        clauses = ["scenic_id = %s"]
        args: List[Any] = [scenic_id]
        if channel:
            clauses.append("channel = %s")
            args.append(channel)
        if target_type:
            clauses.append("target_type = %s")
            args.append(target_type)
        if enabled_only:
            clauses.append("enabled = 1")
        rows = await self.db.fetch_all(
            f"SELECT * FROM `src_opinion_scenic_platform_target` WHERE {' AND '.join(clauses)} ORDER BY id ASC",
            args,
        )
        for row in rows:
            row["extra"] = json.loads(row["extra"]) if row.get("extra") else {}
        return rows

    async def upsert_target(self, data: Dict[str, Any], *, keep_url: bool = False) -> None:
        """新增或更新采集目标。

        keep_url=True 给**自动导入**用（档案导入/挂接、去哪儿导入）：那边带的
        链接是按 POI ID 拼出来的，不能把用户在页面上手工填的景区主页盖掉——
        已有非空 target_url 时保持不动。页面上手工保存走默认的 False，
        填什么存什么，清空也算数。
        """
        url_update = (
            "target_url = IF(target_url IS NULL OR target_url = '', "
            "VALUES(target_url), target_url)"
            if keep_url else "target_url = VALUES(target_url)"
        )
        await self.db.execute(
            f"""
            INSERT INTO `src_opinion_scenic_platform_target`
                (scenic_id, channel, target_type, target_id, target_name, target_url, extra, enabled)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                target_name = VALUES(target_name), {url_update},
                extra = VALUES(extra), enabled = VALUES(enabled)
            """,
            [
                data["scenic_id"], data["channel"], data.get("target_type", "poi"),
                str(data["target_id"]), data.get("target_name"), data.get("target_url"),
                json.dumps(data.get("extra") or {}, ensure_ascii=False),
                int(data.get("enabled", 1)),
            ],
        )

    async def delete_target(self, target_id: int) -> None:
        await self.db.execute("DELETE FROM `src_opinion_scenic_platform_target` WHERE id = %s", [target_id])
