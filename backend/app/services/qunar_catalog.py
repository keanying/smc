"""去哪儿景区目录：拉城市 / 拉 POI / 一键建景区。

这一块解决的是**建景区最烦的那一步**：以前要自己去去哪儿页面上翻出 POI ID，
再手工粘进采集目标；名字打错、ID 少复制一位，都要等到任务跑出 0 条才发现。
现在选城市 → 勾景区 → 建完，ID 是从页面上抓的，不会错。

和采集器的分工：
    collectors/qunar.py   任务调度里跑的**点评采集**
    本文件                 页面上点一下就跑的**目录浏览与导入**，不走任务

共用：同一份 vendor 解析器、同一个 ProxiedClient（代理和指纹一致）。
不共用：这里是用户在页面上等着的，所以失败直接把原因抛给前端，
不像采集那样反复重试——人在等的时候，快点失败比慢慢成功更重要。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.logging import get_logger
from ..db import tables
from ..utils import normalize as nz

logger = get_logger(__name__)

VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor"
BASE_URL = "https://sight.qunar.com"
CHANNEL = "qunar"
#: 省市区县三级数据。去哪儿自己**不提供**省份归属，只有一份扁平城市表，
#: 所以行政区划从公开的静态数据集取，再按名字回接去哪儿城市ID。
REGION_DATA_URL = ("https://raw.githubusercontent.com/kk-418/cn-division"
                   "/main/dist/code/pca.json")

SITE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    "Referer": f"{BASE_URL}/",
}


def _ensure_vendor_on_path() -> None:
    root = str(VENDOR_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _looks_like_verification(html: str) -> bool:
    """只看开头 2000 字：验证页是极简页面，标记一定在最前面。

    全文搜会把正文里出现"验证码"三个字的正常页面误判掉——
    现象是"明明有数据却说被风控了"。
    """
    head = (html or "")[:2000]
    return any(m in head for m in ("访问验证", "安全验证", "请输入验证码"))


class QunarCatalog:
    """去哪儿目录服务。base_url 是实例属性，离线测试指向假站点。"""

    def __init__(self, config, proxy_manager, db=None):
        self.config = config
        self.proxy_manager = proxy_manager
        self.db = db
        self.base_url = BASE_URL

    # ---------------- HTTP ----------------
    def _make_client(self):
        from ..proxy.manager import ProxiedClient
        return ProxiedClient(self.config, self.proxy_manager, CHANNEL,
                             base_headers=dict(SITE_HEADERS))

    async def _get_text(self, url: str, params: Optional[Dict] = None) -> str:
        client = self._make_client()
        async with client:
            html = await client.get_text(url, params=params or {})
        if _looks_like_verification(html):
            raise RuntimeError(
                "去哪儿返回了验证页。稍等几分钟再试；一直这样就是出口 IP 被盯上了，"
                "去系统设置里检查代理配置")
        return html

    async def _get_json(self, url: str) -> Any:
        client = self._make_client()
        async with client:
            return await client.get_json(url)

    # ---------------- 城市 ----------------
    async def cities(self) -> List[Dict[str, str]]:
        _ensure_vendor_on_path()
        from qunar_crawler.parsers import parse_city_index

        html = await self._get_text(f"{self.base_url}/city")
        rows = parse_city_index(html, self.base_url)
        if not rows:
            raise RuntimeError("没解析出城市列表，多半是去哪儿改版了——"
                               "换一份 vendor/qunar_crawler/parsers.py 再试")
        return [c.to_dict() for c in rows]

    # ---------------- 行政区划 ----------------
    async def sync_regions(self, region_url: str = "") -> Dict[str, Any]:
        """拉一份省市区县三级表，并尽力把每一行接到去哪儿城市ID上。

        为什么需要它：去哪儿的城市索引是**一张扁平的名单**，只有"贵阳"，
        没有"贵州省"。没有这张表，导入建出来的景区 province 只能留空，
        景区列表的"地区"列就永远只有半截；想按省份挑城市也无从挑起。

        匹配不上的行政区 `qunar_city_id` 留空——**不猜**。
        同名的区县（全国有一堆"城关区""新华区"）只在**唯一命中**时才接，
        否则宁可留空：接错了比不接更难查，因为它看起来是对的。
        """
        _ensure_vendor_on_path()
        from qunar_crawler.parsers import attach_qunar_cities, flatten_regions, parse_city_index

        url = region_url or REGION_DATA_URL
        html = await self._get_text(f"{self.base_url}/city")
        cities = parse_city_index(html, self.base_url)
        if not cities:
            raise RuntimeError("没解析出城市列表，无法建立行政区划映射")

        payload = await self._get_json(url)
        rows = attach_qunar_cities(flatten_regions(payload, url), cities)
        if not rows:
            raise RuntimeError("行政区划数据是空的，检查 region_url 是否可达")

        if self.db is None:
            return {"total": len(rows), "matched": 0, "saved": 0}

        now = datetime.now()
        cols = ["province_code", "province_name", "city_code", "city_name",
                "district_code", "district_name", "qunar_city_id",
                "qunar_city_name", "source_url", "sync_time"]
        updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols
                            if c not in ("province_code", "city_code", "district_code"))
        sql = (f"INSERT INTO `{tables.QUNAR_REGION}` "
               f"({', '.join('`' + c + '`' for c in cols)}) "
               f"VALUES ({', '.join(['%s'] * len(cols))}) "
               f"ON DUPLICATE KEY UPDATE {updates}")
        # 一行一行插四千多条会把页面卡住，分批。
        batch: List[List[Any]] = []
        saved = 0
        for row in rows:
            batch.append([
                nz.truncate(nz.to_text(row.get("province_code")), 20),
                nz.truncate(nz.to_text(row.get("province_name")), 50),
                nz.truncate(nz.to_text(row.get("city_code")), 20),
                nz.truncate(nz.to_text(row.get("city_name")), 50),
                nz.truncate(nz.to_text(row.get("district_code")), 20),
                nz.truncate(nz.to_text(row.get("district_name")), 50),
                nz.truncate(nz.to_text(row.get("qunar_city_id")), 100),
                nz.truncate(nz.to_text(row.get("qunar_city_name")), 100),
                nz.truncate(nz.to_text(row.get("source_url")), 500),
                now,
            ])
            if len(batch) >= 500:
                await self.db.execute_many(sql, batch)
                saved += len(batch)
                batch = []
        if batch:
            await self.db.execute_many(sql, batch)
            saved += len(batch)

        matched = sum(1 for r in rows if r.get("qunar_city_id"))
        logger.info("[去哪儿] 行政区划同步完成：%d 行，其中 %d 行接上了去哪儿城市",
                    saved, matched)
        return {"total": len(rows), "matched": matched, "saved": saved}

    async def provinces(self) -> List[Dict[str, Any]]:
        """省份 → 该省下能对上去哪儿城市的城市列表。导入对话框的省筛选用。

        只返回**接得上去哪儿**的城市：列一堆点了没反应的城市名，
        比不列更让人困惑。
        """
        if self.db is None:
            return []
        rows = await self.db.fetch_all(
            f"SELECT province_name, qunar_city_id, "
            f"       MIN(qunar_city_name) AS qunar_city_name "
            f"FROM `{tables.QUNAR_REGION}` "
            f"WHERE qunar_city_id <> '' AND province_name <> '' "
            f"GROUP BY province_name, qunar_city_id "
            f"ORDER BY province_name ASC, qunar_city_name ASC")
        grouped: Dict[str, List[Dict[str, str]]] = {}
        for row in rows:
            grouped.setdefault(row["province_name"], []).append(
                {"city_id": row["qunar_city_id"],
                 "city_name": row["qunar_city_name"]})
        return [{"province": name, "cities": cities}
                for name, cities in grouped.items()]

    async def province_of(self, city_id: str) -> str:
        """这个去哪儿城市属于哪个省。没同步过行政区划就返回空字符串。"""
        if self.db is None or not city_id:
            return ""
        return nz.to_text(await self.db.fetch_value(
            f"SELECT province_name FROM `{tables.QUNAR_REGION}` "
            f"WHERE qunar_city_id = %s AND province_name <> '' LIMIT 1",
            [city_id], ""))

    # ---------------- POI ----------------
    async def pois(self, city_id: str, page: int = 1) -> Dict[str, Any]:
        """列一页某城市的景区。只取列表页，不进详情页。

        为什么不顺手把详情也拉了：一页十几个 POI，逐个进详情就是十几次请求、
        十几秒，而用户此刻只是想**看看这个城市有哪些景区**。
        详情留到导入时按需拉（见 import 接口的 with_detail）。
        """
        _ensure_vendor_on_path()
        from qunar_crawler.parsers import parse_poi_list

        html = await self._get_text(f"{self.base_url}/list",
                                    {"city": city_id, "page": page})
        parsed = parse_poi_list(html)
        return {
            "city_id": city_id,
            "city_name": parsed.city_name,
            "page": page,
            "total_pages": parsed.total_pages or 1,
            "items": [
                {"poi_id": p.poi_id, "poi_name": p.poi_name,
                 "address": p.address, "scenic_level": p.scenic_level}
                for p in parsed.items
            ],
        }

    async def poi_detail(self, poi_id: str, city_id: str = "",
                         city_name: str = "") -> Dict[str, Any]:
        _ensure_vendor_on_path()
        from qunar_crawler.parsers import parse_poi_detail

        html = await self._get_text(f"{self.base_url}/{poi_id}")
        record = parse_poi_detail(html, poi_id, city_id=city_id, city_name=city_name)
        data = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        data["source_url"] = f"{self.base_url}/{poi_id}"
        return data

    # ---------------- 落库 ----------------
    async def save_poi_info(self, row: Dict[str, Any], scenic_id: str = "") -> None:
        if self.db is None:
            return
        fields = {
            "channel": CHANNEL,
            "poi_id": nz.to_text(row.get("poi_id")),
            "scenic_id": scenic_id,
            "poi_name": nz.truncate(nz.to_text(row.get("poi_name")), 200),
            "city_id": nz.truncate(nz.to_text(row.get("city_id")), 100),
            "city_name": nz.truncate(nz.to_text(row.get("city_name")), 100),
            "address": nz.truncate(nz.to_text(row.get("address")), 500),
            "open_time": nz.to_text(row.get("open_time")),
            "tel": nz.truncate(nz.to_text(row.get("tel")), 200),
            "scenic_level": nz.truncate(nz.to_text(row.get("scenic_level")), 50),
            "scenic_intro": nz.to_text(row.get("scenic_intro")),
            "discount_policy": nz.to_text(row.get("discount_policy")),
            "amenity": nz.to_text(row.get("amenity")),
            "source_url": nz.truncate(nz.to_text(row.get("source_url")), 500),
            "crawl_time": datetime.now(),
        }
        cols = list(fields)
        # ⚠️ scenic_id 单独处理：只是刷新档案（没带 scenic_id）时，
        #    **不能**把已经建立的景区关联清掉。
        updates = ", ".join(
            f"`{c}` = VALUES(`{c}`)" if c != "scenic_id"
            else "`scenic_id` = IF(VALUES(`scenic_id`) = '', `scenic_id`, VALUES(`scenic_id`))"
            for c in cols if c not in ("channel", "poi_id")
        )
        await self.db.execute(
            f"INSERT INTO `{tables.SCENIC_POI_INFO}` "
            f"({', '.join('`' + c + '`' for c in cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON DUPLICATE KEY UPDATE {updates}",
            [fields[c] for c in cols],
        )

    async def list_saved(self, *, city_id: str = "", scenic_id: str = "",
                         keyword: str = "", page: int = 1,
                         page_size: int = 20) -> Dict[str, Any]:
        if self.db is None:
            return {"total": 0, "page": page, "page_size": page_size, "items": []}
        clauses = ["channel = %s"]
        args: List[Any] = [CHANNEL]
        if city_id:
            clauses.append("city_id = %s"); args.append(city_id)
        if scenic_id:
            clauses.append("scenic_id = %s"); args.append(scenic_id)
        if keyword:
            clauses.append("(poi_name LIKE %s OR address LIKE %s)")
            args += [f"%{keyword}%", f"%{keyword}%"]
        where = " AND ".join(clauses)
        total = await self.db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{tables.SCENIC_POI_INFO}` WHERE {where}",
            args, 0)
        rows = await self.db.fetch_all(
            f"SELECT * FROM `{tables.SCENIC_POI_INFO}` WHERE {where} "
            f"ORDER BY city_id ASC, poi_name ASC LIMIT %s OFFSET %s",
            [*args, int(page_size), max(0, (page - 1) * page_size)])
        return {"total": int(total or 0), "page": page,
                "page_size": page_size, "items": rows}
