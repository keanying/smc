"""携程景区档案：列表 + 详情。

移植自用户给的 crawl_ctrip_scenics.py，接口和字段映射**一比一照搬**，
改掉的只有三处，每一处都有理由：

1. requests + 线程池 → ProxiedClient：档案采集和点评采集共用同一套
   快代理 IP 池和轮换策略。原脚本裸连，一个 IP 打两万条必被风控。
2. SQLite 落库 → 直接进 MySQL 的档案表（幂等 upsert）。
3. 分页的 excludePoiIdList 加了上限保护，见下面 MAX_EXCLUDE。

⚠️ 这个接口的分页方式很特别：不是 page=1,2,3，而是**"把我已经拿到的
   ID 全发过去，你给我别的"**。所以请求体会随着采集进度越来越大——
   一个省两千条，最后一次请求就带着两千个 ID。原脚本跑全国两万条，
   请求体能到 160KB。超过 MAX_EXCLUDE 就停下报错，而不是继续发一个
   越来越大的包直到被服务端拒绝（那时的报错信息完全看不出原因）。
"""
from __future__ import annotations

import asyncio
import copy
import html
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from ...core.logging import get_logger
from .base import ArchiveRow, ArchiveSource, ProgressFn, Region, register

logger = get_logger(__name__)

LIST_URL = "https://m.ctrip.com/restapi/soa2/18109/json/getAttractionList"
DETAIL_URL = "https://m.ctrip.com/restapi/soa2/18254/json/getPoiMoreDetail"

#: 携程的"全国"。这是用户脚本里唯一验证过能用的区域编号，
#: 所以它永远是区域清单里的第一条——即使自动探测省份失败，
#: 携程档案也能直接开采。
NATIONWIDE_ID = "110000"
NATIONWIDE_NAME = "全国"

#: excludePoiIdList 的上限，超了就停。详见模块注释。
MAX_EXCLUDE = 6000
#: 一轮并发拉多少页。原脚本是 15，这里保守到 8——它裸连不过代理，
#: 我们每个请求都要过代理出口，并发太高纯粹是自己挤自己。
PAGES_PER_BATCH = 8
#: 连续多少轮没有新增就认输。原脚本是 4。
MAX_STALL = 3

LIST_BASE: Dict[str, Any] = {
    "index": 1, "count": 20, "sortType": 1, "isShowAggregation": True,
    "excludePoiIdList": [], "districtId": NATIONWIDE_ID, "scene": "DISTRICT",
    "pageId": "214062", "traceId": "", "crnVersion": "2020-09-01 22:00:45",
    "extension": [{"name": "osVersion", "value": "18.0"},
                  {"name": "deviceType", "value": "ios"}],
    "extendMap": [{"key": "LIST_VERSION", "value": "8.68.6"},
                  {"key": "COMPONENT_VERSION", "value": "8.84.0"},
                  {"key": "needRightPackage", "value": "V1"}],
    "filter": {"filterItems": []}, "isInitialState": False,
    "head": {"cid": "", "ctok": "", "cver": "1.0", "lang": "01", "sid": "8888",
             "syscode": "09", "auth": "", "xsid": "",
             "extension": [{"name": "fromChannel", "value": ""}]},
    "returnModuleType": "product",
}

DETAIL_BASE: Dict[str, Any] = {
    "poiId": 0, "scene": "basic",
    "head": {"cid": "", "ctok": "", "cver": "1.0", "lang": "01", "sid": "8888",
             "syscode": "09", "auth": "", "xsid": "", "extension": []},
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(value: Any) -> str:
    if not value:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(str(value), "html.parser")
    for tag in soup(["script", "style", "img"]):
        tag.decompose()
    return clean_text(soup.get_text(" ", strip=True))


# ---------------------------------------------------------------- 解析
def parse_list_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """列表页 → POI 行。card 有时是对象，有时是 cardStr 里的 JSON 字符串。"""
    output: List[Dict[str, Any]] = []
    for entry in data.get("attractionList") or []:
        card = entry.get("card")
        if not card and entry.get("cardStr"):
            try:
                card = json.loads(entry["cardStr"])
            except (TypeError, ValueError):
                card = None
        if not isinstance(card, dict) or not card.get("poiId"):
            continue
        output.append({
            "poi_id": str(card["poiId"]),
            "city_id": str(card.get("districtId") or ""),
            "city_name": clean_text(card.get("districtName")),
            "poi_name": clean_text(card.get("poiName")),
            "address": clean_text(card.get("address")),
            "scenic_level": clean_text(card.get("sightLevelStr")),
        })
    return output


def parse_regions(data: Dict[str, Any]) -> List[Region]:
    """从列表响应的聚合块里挖省份。

    ⚠️ 这一段**没有对着线上接口验证过**（开发环境连不上携程）。
    挖不到就返回空，调用方会退回到"只有全国这一条"，
    页面上仍然可以手工添加区域，不会把功能卡死。
    """
    out: List[Region] = []
    seen = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(out) > 200:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        rid = node.get("districtId") or node.get("id") or node.get("code")
        name = node.get("districtName") or node.get("name") or node.get("title")
        kind = str(node.get("type") or node.get("level") or "").lower()
        if rid and name and ("province" in kind or "province" in str(node.get("nodeType") or "").lower()):
            key = str(rid)
            if key not in seen:
                seen.add(key)
                out.append(Region(region_id=key, region_name=clean_text(name)))
        for value in node.values():
            walk(value, depth + 1)

    walk(data.get("aggregationList") or data.get("aggregation") or [])
    return out


def _find_module(templates: Any, module_name: str) -> Optional[Dict[str, Any]]:
    for template in templates or []:
        for module in template.get("moduleList") or []:
            if module.get("moduleName") == module_name:
                return module
    return None


def format_open_time(module: Optional[Dict[str, Any]]) -> str:
    if not module:
        return ""
    data = module.get("poiOpenModule") or {}
    parts: List[str] = []
    for rule in data.get("openDateRuleInfo") or []:
        date_desc = clean_text(rule.get("dateDesc"))
        weeks = [clean_text(i.get("weekDesc")) for i in rule.get("openWeekRuleInfo") or []
                 if clean_text(i.get("weekDesc"))]
        value = "；".join(weeks)
        if date_desc and value:
            parts.append(f"{date_desc} {value}")
        elif date_desc or value:
            parts.append(date_desc or value)
    for key in ("openTime", "openTimeDesc", "openDesc", "businessTime"):
        value = clean_text(data.get(key))
        if value and value not in parts:
            parts.append(value)
    return "；".join(dict.fromkeys(parts))


def format_tel(module: Optional[Dict[str, Any]]) -> str:
    if not module:
        return ""
    numbers: List[str] = []
    for raw in (module.get("poiBasicModule") or {}).get("telephoneList") or []:
        number = clean_text(raw)
        if number.startswith("+86") and len(number) > 6:
            number = number[3:]
        if number and number not in numbers:
            numbers.append(number)
    return "; ".join(numbers)


def format_policy(module: Optional[Dict[str, Any]]) -> str:
    if not module:
        return ""
    data = module.get("preferentialModule") or {}
    groups: List[str] = []
    for policy in data.get("policyInfoList") or []:
        title = clean_text(policy.get("customDesc"))
        details: List[str] = []
        for item in policy.get("policyDetail") or []:
            limitation = clean_text(item.get("limitation"))
            description = clean_text(item.get("policyDesc"))
            if limitation and description:
                details.append(f"{limitation}（{description}）")
            elif limitation or description:
                details.append(limitation or description)
        if title and details:
            groups.append(f"{title}：{'；'.join(details)}")
        elif title or details:
            groups.append(title or "；".join(details))
    note = clean_text(data.get("additionalNote"))
    if note:
        groups.append(f"补充说明：{note}")
    return " | ".join(groups)


def format_amenity(module: Optional[Dict[str, Any]]) -> str:
    if not module:
        return ""
    outer = module.get("serviceFacilityModule") or {}
    facilities = outer.get("serviceFacilityModule") or outer.get("facilityList") or []
    output: List[str] = []
    for facility in facilities:
        name = clean_text(facility.get("facilityName"))
        descriptions: List[str] = []
        for info in facility.get("facilityInfoList") or []:
            items: List[str] = []
            seen_pairs = set()
            for item in info.get("facilityItemList") or []:
                label = clean_text(item.get("itemName"))
                value = clean_text(item.get("itemValue"))
                if value and (label, value) not in seen_pairs:
                    seen_pairs.add((label, value))
                    items.append(f"{label}={value}" if label else value)
            if items:
                descriptions.append("，".join(items))
        if name and descriptions:
            output.append(f"{name}：{'；'.join(descriptions)}")
        elif name or descriptions:
            output.append(name or "；".join(descriptions))
    return " | ".join(output)


def parse_detail(data: Dict[str, Any]) -> Dict[str, str]:
    templates = data.get("templateList") or []
    introduction = ""
    intro_module = _find_module(templates, "图文详情")
    if intro_module:
        introduction = html_to_text(
            (intro_module.get("introductionModule") or {}).get("introduction"))
    return {
        "open_time": format_open_time(_find_module(templates, "开放时间")),
        "tel": format_tel(_find_module(templates, "基础信息")),
        "scenic_intro": introduction,
        "discount_policy": format_policy(_find_module(templates, "优待政策")),
        "amenity": format_amenity(_find_module(templates, "服务设施")),
    }


# ---------------------------------------------------------------- 采集源
@register
class CtripArchiveSource(ArchiveSource):
    channel = "ctrip"
    label = "携程"
    supports_detail = True
    base_headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://m.ctrip.com",
        "Referer": "https://m.ctrip.com/webapp/you/sight/china110000.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    list_url = LIST_URL      # 测试可覆盖
    detail_url = DETAIL_URL

    def _list_payload(self, region_id: str, page: int,
                      exclude: List[str], cid: str) -> Dict[str, Any]:
        payload = copy.deepcopy(LIST_BASE)
        payload["districtId"] = region_id or NATIONWIDE_ID
        payload["index"] = page
        payload["excludePoiIdList"] = [int(x) for x in exclude if str(x).isdigit()]
        payload["traceId"] = str(uuid.uuid4())
        payload["isInitialState"] = not exclude and page == 1
        payload["head"]["cid"] = cid
        return payload

    async def regions(self) -> List[Region]:
        """全国 + （尽力挖到的）省份。

        全国那条是写死的：它是用户脚本里验证过能用的编号，
        所以哪怕省份一个都挖不到，携程档案也能直接开采。
        """
        found: List[Region] = [Region(NATIONWIDE_ID, NATIONWIDE_NAME, level="nation")]
        try:
            data = await self.post_json(
                self.list_url,
                self._list_payload(NATIONWIDE_ID, 1, [], uuid.uuid4().hex[:20]))
            for region in parse_regions(data or {}):
                if region.region_id != NATIONWIDE_ID:
                    found.append(region)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[携程档案] 省份探测失败，只给全国这一条：%s", exc)
        return found

    async def collect_region(self, region: Region, *,
                             progress: Optional[ProgressFn] = None,
                             limit: int = 0) -> List[ArchiveRow]:
        cid = uuid.uuid4().hex[:20]
        seen: Dict[str, ArchiveRow] = {}
        order: List[str] = []
        stalled = 0

        while True:
            if limit and len(seen) >= limit:
                break
            if len(order) > MAX_EXCLUDE:
                logger.warning(
                    "[携程档案] %s 已采 %d 条，排除清单超过 %d 的上限，"
                    "本区域到此为止——再发下去请求体会大到被服务端拒绝，"
                    "那时报错完全看不出原因。要采全就按省分开采。",
                    region.region_name, len(order), MAX_EXCLUDE)
                break

            payloads = [self._list_payload(region.region_id, page, order, cid)
                        for page in range(1, PAGES_PER_BATCH + 1)]
            results = await asyncio.gather(
                *[self.post_json(self.list_url, p) for p in payloads],
                return_exceptions=True)

            added = 0
            errors = 0
            for result in results:
                if isinstance(result, Exception):
                    errors += 1
                    continue
                for item in parse_list_response(result or {}):
                    pid = item["poi_id"]
                    if pid in seen:
                        continue
                    seen[pid] = ArchiveRow(
                        poi_id=pid, poi_name=item["poi_name"],
                        province=region.region_name if region.level == "province" else "",
                        city_id=item["city_id"], city_name=item["city_name"],
                        address=item["address"], scenic_level=item["scenic_level"],
                        source_url=f"https://you.ctrip.com/sight/{pid}.html",
                        region_id=region.region_id,
                    )
                    order.append(pid)
                    added += 1
                    if limit and len(seen) >= limit:
                        break
                if limit and len(seen) >= limit:
                    break

            if progress:
                progress(len(seen), f"{region.region_name} 已采 {len(seen)} 条")

            if added == 0:
                stalled += 1
                # 全是请求错误时多等一会儿再判死，别把一次网络抖动当成"采完了"
                if errors:
                    await asyncio.sleep(min(20, 5 * stalled))
                if stalled >= MAX_STALL:
                    break
            else:
                stalled = 0

        return [seen[pid] for pid in order]

    async def fetch_detail(self, poi_id: str) -> Dict[str, str]:
        payload = copy.deepcopy(DETAIL_BASE)
        payload["poiId"] = int(poi_id) if str(poi_id).isdigit() else poi_id
        payload["head"]["cid"] = uuid.uuid4().hex[:20]
        data = await self.post_json(self.detail_url, payload)
        if not data:
            raise RuntimeError("详情接口没有返回内容")
        if data.get("result") not in (0, None):
            raise RuntimeError(f"详情接口 result={data.get('result')}")
        if not data.get("templateList"):
            raise RuntimeError("详情响应里没有 templateList")
        return parse_detail(data)
