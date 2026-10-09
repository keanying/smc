"""去哪儿景区档案：包在已有的 QunarCatalog 外面。

解析逻辑一行没重写——`services/qunar_catalog.py` 那套（vendor 解析器 +
验证页保护 + 翻页停止条件）已经跑过真实页面验证，这里只是把它
接到档案的统一接口上，好让三个渠道在页面上长得一样。

去哪儿的"区域"是**城市**不是省份：它的城市索引本来就是一张扁平名单
（见 qunar_catalog 的注释）。所以 level 写 city，档案页上按省筛选
靠的是行政区划表回填的 province 字段。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ...core.logging import get_logger
from .base import ArchiveRow, ArchiveSource, ProgressFn, Region, register

logger = get_logger(__name__)


@register
class QunarArchiveSource(ArchiveSource):
    channel = "qunar"
    label = "去哪儿"
    supports_detail = True

    def __init__(self, config, proxy_manager, db=None):
        super().__init__(config, proxy_manager, db)
        from ..qunar_catalog import QunarCatalog
        self.catalog = QunarCatalog(config, proxy_manager, db)

    async def regions(self) -> List[Region]:
        rows = await self.catalog.cities()
        return [Region(region_id=r["city_id"], region_name=r["city_name"],
                       level="city")
                for r in rows if r.get("city_id")]

    async def collect_region(self, region: Region, *,
                             progress: Optional[ProgressFn] = None,
                             limit: int = 0) -> List[ArchiveRow]:
        seen: Dict[str, ArchiveRow] = {}
        page = 1
        pages = 1
        province = await self.catalog.province_of(region.region_id)
        while page <= pages:
            if limit and len(seen) >= limit:
                break
            data = await self.catalog.pois(region.region_id, page)
            pages = int(data.get("total_pages") or 1)
            items = data.get("items") or []
            if not items:
                break
            before = len(seen)
            for item in items:
                poi_id = str(item.get("poi_id") or "")
                if not poi_id or poi_id in seen:
                    continue
                seen[poi_id] = ArchiveRow(
                    poi_id=poi_id,
                    poi_name=str(item.get("poi_name") or ""),
                    province=province,
                    city_id=region.region_id,
                    city_name=data.get("city_name") or region.region_name,
                    address=str(item.get("address") or ""),
                    scenic_level=str(item.get("scenic_level") or ""),
                    source_url=f"{self.catalog.base_url}/{poi_id}",
                    region_id=region.region_id,
                )
            if len(seen) == before:
                # 去哪儿翻过最后一页会把最后一页**再发一遍**而不是返回空页，
                # 只按"空页停"会死循环。没有新增就是到头了。
                break
            if progress:
                progress(len(seen),
                         f"{region.region_name} 第 {page}/{pages} 页，已采 {len(seen)} 条")
            page += 1
        return list(seen.values())

    async def fetch_detail(self, poi_id: str) -> Dict[str, str]:
        data = await self.catalog.poi_detail(poi_id)
        return {
            "open_time": str(data.get("open_time") or ""),
            "tel": str(data.get("tel") or ""),
            "scenic_intro": str(data.get("scenic_intro") or ""),
            "discount_policy": str(data.get("discount_policy") or ""),
            "amenity": str(data.get("amenity") or ""),
        }
