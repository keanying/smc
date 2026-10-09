"""同程景区档案：只有列表，没有详情页。

移植自用户给的「同程采集城市和景区信息.py」，接口、正则、字段一比一照搬，
改掉的是这几处：

1. requests + 线程池 → ProxiedClient（和点评采集共用同一套代理池）。
2. `pid_cid_map.json` 这个外部文件 → **随包带一份**（同目录的
   `tongcheng_regions.json`，34 个省 + 358 个城市），不再依赖
   `/home/ubuntu/tc/` 这种本机路径。抓不到时还会退回到解析首页。
3. CSV/JSONL → MySQL 档案表（幂等 upsert）。

⚠️ **同程没有景区详情页**，所以档案里的开放时间 / 电话 / 优待政策 /
   服务设施 / 介绍这几列对同程**永远是空的**。这不是采失败——
   列表页就只给名称、等级、地址、省、市这五样。detail_status 会写
   `unsupported`，免得有人对着一屏"待采集"反复重试。

区域清单有三条路，按可信度从高到低：
   1. 随包的 `tongcheng_regions.json`（用户从同程页面上导出的真实映射）
   2. 解析 ly.com 首页的城市选择器（**没对着线上验证过**，兜底用）
   3. 页面上手工添加一条（平台改版时的最后一道保险）

同程的列表接口除了 pid 还吃 cid（`pid=6&cid=80`），所以区域清单里
省和城市都给：广东一个省九百多条，按城市拆开采更容易断点续采。
城市的 region_id 写成 `pid:cid`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...core.logging import get_logger
from .base import ArchiveRow, ArchiveSource, ProgressFn, Region, register

logger = get_logger(__name__)

BASE_URL = "https://www.ly.com"
LIST_URL = f"{BASE_URL}/scenery/NewSearchList.aspx"
HOME_URL = f"{BASE_URL}/scenery/"

#: 一页 10 条，这是接口定死的，不是我们选的
PAGE_SIZE = 10

#: 随包的 pid/cid 映射。来自同程页面上导出的真实数据，
#: 比解析首页可靠得多——首页结构会变，这份不会。
BUNDLED_REGIONS = Path(__file__).resolve().parent / "tongcheng_regions.json"


def load_bundled_regions() -> List[Region]:
    """读随包的映射：34 个省 + 358 个城市。

    城市的 region_id 是 `pid:cid`——采集时要同时带上这两个参数，
    只给 cid 同程是不认的。
    """
    if not BUNDLED_REGIONS.exists():
        return []
    try:
        data: Dict[str, Any] = json.loads(
            BUNDLED_REGIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[同程档案] 随包区域映射读不了：%s", exc)
        return []

    out: List[Region] = []
    for province in data.get("provinces") or []:
        pid = str(province.get("pid") or "").strip()
        name = str(province.get("province_name") or "").strip()
        if not pid or not name:
            continue
        out.append(Region(region_id=pid, region_name=name, level="province"))
        for city in province.get("cities") or []:
            cid = str(city.get("cid") or "").strip()
            city_name = str(city.get("city_name") or "").strip()
            if not cid or cid == "0" or not city_name:
                continue
            out.append(Region(region_id=f"{pid}:{cid}",
                              region_name=f"{name}·{city_name}", level="city"))
    return out


def split_region_id(region_id: str) -> tuple:
    """`6` → ('6', '0')；`6:80` → ('6', '80')。"""
    text = str(region_id or "").strip()
    if ":" in text:
        pid, _, cid = text.partition(":")
        return pid.strip(), (cid.strip() or "0")
    return text, "0"

ITEM_RE = re.compile(r'<div class="scenery_list">.*?(?=<div class="scenery_list">|$)', re.S)
SID_RE = re.compile(r'class="s_info" sid="(\d+)"')
NAME_RE = re.compile(r'class="sce_name[^"]*"[^>]*\s+title="([^"]+)"')
CITY_RE = re.compile(r'title="([^"]+)旅游景点"')
LEVEL_RE = re.compile(r'class="s_level">([^<]*)<')
ADDR_RE = re.compile(r'地址：([^<]*)</p>')
PAGES_RE = re.compile(r'pageNumber" value="(\d+)"')

#: 省份清单的几种可能写法，从最明确的往下退。
#: 线上到底是哪一种没法在这里验证，所以多准备几条，全都不中就返回空。
PROVINCE_PATTERNS = (
    re.compile(r'pid=(\d+)[^>]*>\s*([一-龥]{2,10})\s*<'),
    re.compile(r'data-pid="(\d+)"[^>]*>\s*([一-龥]{2,10})\s*<'),
    re.compile(r'<a[^>]+href="[^"]*[?&]pid=(\d+)[^"]*"[^>]*title="([一-龥]{2,10})"'),
)


def parse_provinces(html: str) -> List[Region]:
    """从景点首页的城市选择器里挖出 pid → 省名。

    同一个 pid 可能被多种写法匹配到，按 pid 去重，**先匹配到的算**
    （模式是按可信度排的）。
    """
    found: Dict[str, str] = {}
    for pattern in PROVINCE_PATTERNS:
        for pid, name in pattern.findall(html or ""):
            if pid not in found and name:
                found[pid] = name.strip()
    return [Region(region_id=pid, region_name=name)
            for pid, name in sorted(found.items(), key=lambda kv: int(kv[0]))]


def parse_page(html: str, province: str, pid: str) -> List[ArchiveRow]:
    """解析一页列表。正则和用户脚本里的一模一样。"""
    rows: List[ArchiveRow] = []
    for block in ITEM_RE.findall(html or ""):
        m_sid = SID_RE.search(block)
        m_name = NAME_RE.search(block)
        if not (m_sid and m_name):
            continue
        # 城市取**最后一个**"xx旅游景点"链接：面包屑是 省 > 市，
        # 取第一个会把省名当成城市名。
        cities = CITY_RE.findall(block)
        m_level = LEVEL_RE.search(block)
        m_addr = ADDR_RE.search(block)
        sid = m_sid.group(1)
        rows.append(ArchiveRow(
            poi_id=sid,
            poi_name=m_name.group(1).strip(),
            province=province,
            city_name=(cities[-1] if cities else ""),
            scenic_level=(m_level.group(1).strip() if m_level else ""),
            address=(m_addr.group(1).strip() if m_addr else ""),
            source_url=f"{BASE_URL}/scenery/BookSceneryTicket_{sid}.html",
            region_id=pid,
        ))
    return rows


def total_pages(html: str) -> int:
    m = PAGES_RE.search(html or "")
    return int(m.group(1)) if m else 0


@register
class TongchengArchiveSource(ArchiveSource):
    channel = "tongcheng"
    label = "同程"
    #: 列表页就是全部——同程没有景区详情页
    supports_detail = False
    base_headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": HOME_URL,
    }

    list_url = LIST_URL      # 测试可覆盖
    home_url = HOME_URL

    async def regions(self) -> List[Region]:
        """随包映射优先；没有才去解析首页。

        为什么不是反过来（先拉线上、随包的当兜底）：首页解析这条路
        **没有对着线上验证过**，让它盖掉一份已知正确的映射，
        风险方向是错的——解析出一份残缺清单，用户拿去采，
        采回来一片空，还不知道问题出在清单上。
        """
        bundled = load_bundled_regions()
        if bundled:
            return bundled
        html = await self.get_text(self.home_url)
        found = parse_provinces(html)
        if not found:
            raise RuntimeError(
                "没从同程景点首页解析出省份清单——多半是页面结构变了。"
                "可以在区域清单里手工添加（pid + 省名），手工加完照样能采。")
        return found

    async def _fetch_page(self, region_id: str, page: int) -> str:
        pid, cid = split_region_id(region_id)
        return await self.get_text(self.list_url, {
            "action": "getlist", "page": page, "pid": pid,
            "cid": cid, "cyid": 0, "isnow": 0, "IsNJL": 0,
        })

    async def collect_region(self, region: Region, *,
                             progress: Optional[ProgressFn] = None,
                             limit: int = 0) -> List[ArchiveRow]:
        first = await self._fetch_page(region.region_id, 1)
        pages = total_pages(first)
        seen: Dict[str, ArchiveRow] = {}
        # 城市级区域的名字是「广东·广州」，province 列只要省那一段
        province = region.region_name.split("·")[0]

        def take(rows: List[ArchiveRow]) -> None:
            for row in rows:
                seen.setdefault(row.poi_id, row)

        take(parse_page(first, province, region.region_id))
        if progress:
            progress(len(seen), f"{region.region_name} 共 {pages} 页，已采 {len(seen)} 条")

        page = 2
        while page <= pages:
            if limit and len(seen) >= limit:
                break
            html = await self._fetch_page(region.region_id, page)
            rows = parse_page(html, province, region.region_id)
            if not rows:
                # 空页就停：同程翻过头会返回一个没有 scenery_list 的页面，
                # 继续翻下去只是在白跑请求。
                logger.info("[同程档案] %s 第 %d 页没有内容，提前收尾",
                            region.region_name, page)
                break
            take(rows)
            if progress:
                progress(len(seen),
                         f"{region.region_name} 第 {page}/{pages} 页，已采 {len(seen)} 条")
            page += 1

        return list(seen.values())
