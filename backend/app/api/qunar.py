"""去哪儿景区目录接口：浏览城市 / 景区，并一键建成本系统的景区。

为什么值得有这组接口：建景区最烦的一步是**找 POI ID**——去去哪儿页面上
翻出来、复制、粘进采集目标，名字打错或者 ID 少一位，要等到任务跑出 0 条
才发现。这里选城市 → 勾景区 → 建完，ID 是从页面上抓的，不会错。

    GET  /api/qunar/cities            城市索引（去哪儿的城市 slug）
    GET  /api/qunar/pois              某城市的景区列表（分页）
    GET  /api/qunar/pois/{poi_id}     单个景区的详细档案
    POST /api/qunar/import            勾选的景区 → 建景区 + 建采集目标 + 存档案
    GET  /api/qunar/saved             已经存下来的档案（可按城市/景区筛）
    GET  /api/qunar/provinces         省份 → 城市（导入对话框按省筛选用）
    POST /api/qunar/regions/sync      同步一次省市区县行政区划
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, Query

from ..core.logging import get_logger
from ..services.qunar_catalog import QunarCatalog
from .common import bad_request, ok
from .deps import AppState, get_state

logger = get_logger(__name__)
router = APIRouter(prefix="/api/qunar", tags=["去哪儿目录"])


def _catalog(state: AppState) -> QunarCatalog:
    return QunarCatalog(state.config, state.proxy_manager, state.db)


@router.get("/cities")
async def cities(state: AppState = Depends(get_state)):
    try:
        return ok(await _catalog(state).cities())
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))


@router.get("/pois")
async def pois(city: str = Query(..., description="去哪儿城市标识，如 guiyang"),
               page: int = 1, state: AppState = Depends(get_state)):
    try:
        return ok(await _catalog(state).pois(city, page))
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))


@router.get("/pois/{poi_id}")
async def poi_detail(poi_id: str, city: str = "", city_name: str = "",
                     save: bool = False, state: AppState = Depends(get_state)):
    catalog = _catalog(state)
    try:
        data = await catalog.poi_detail(poi_id, city, city_name)
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))
    if save:
        await catalog.save_poi_info(data)
    return ok(data)


@router.get("/saved")
async def saved(city: str = "", scenic_id: str = "", keyword: str = "",
                page: int = 1, page_size: int = 20,
                state: AppState = Depends(get_state)):
    return ok(await _catalog(state).list_saved(
        city_id=city, scenic_id=scenic_id, keyword=keyword,
        page=page, page_size=page_size))


@router.post("/import")
async def import_pois(payload: Dict[str, Any] = Body(...),
                      state: AppState = Depends(get_state)):
    """把勾选的去哪儿景区建成本系统的景区。

    payload:
      items: [{poi_id, poi_name, address?, scenic_level?}]
      city_id / city_name:  这一批来自哪个城市
      with_detail:  是否顺便进详情页补开放时间/电话/介绍等（慢，但档案更全）
      scenic_prefix: 生成的景区ID前缀，默认 QN

    ⚠️ 景区ID用 `{前缀}{poi_id}` 而不是自增号：重复导入同一个景区时
       会命中同一个 ID 直接更新，不会建出两条内容一样、ID 不同的景区。
       这个系统里景区ID是所有数据的归属键，重了之后数据会被劈成两半。
    """
    items: List[Dict[str, Any]] = payload.get("items") or []
    if not items:
        raise bad_request("没有勾选任何景区")
    city_id = str(payload.get("city_id") or "")
    city_name = str(payload.get("city_name") or "")
    with_detail = bool(payload.get("with_detail"))
    prefix = str(payload.get("scenic_prefix") or "QN").strip() or "QN"

    catalog = _catalog(state)
    # 整批景区来自同一个城市，省份查一次就够，不用每条查一遍。
    province = await catalog.province_of(city_id)
    created, updated, failed = 0, 0, []
    for item in items:
        poi_id = str(item.get("poi_id") or "").strip()
        poi_name = str(item.get("poi_name") or "").strip()
        if not poi_id or not poi_name:
            failed.append({"poi_id": poi_id, "error": "缺少 poi_id 或名称"})
            continue
        scenic_id = f"{prefix}{poi_id}"
        row: Dict[str, Any] = {
            "poi_id": poi_id, "poi_name": poi_name,
            "city_id": city_id, "city_name": city_name,
            "address": item.get("address") or "",
            "scenic_level": item.get("scenic_level") or "",
            "source_url": f"{catalog.base_url}/{poi_id}",
        }
        if with_detail:
            try:
                detail = await catalog.poi_detail(poi_id, city_id, city_name)
                # ⚠️ 详情只**补**列表页没有的字段，不覆盖已有的。
                #    尤其是 poi_name：用户在列表上勾的是"青岩古镇"，
                #    详情页万一解析偏了（改版、串页、拿到相邻景区的名字），
                #    建出来的景区就叫成别的名字了，而用户完全不知道为什么。
                #    他看见什么就该建成什么。
                for key, value in detail.items():
                    if value in (None, ""):
                        continue
                    if not row.get(key):
                        row[key] = value
            except Exception as exc:  # noqa: BLE001
                # 详情拉不到**不该让整条导入失败**：名字和 POI ID 已经够建景区、
                # 够跑采集了，档案可以之后再刷。
                logger.warning("[去哪儿] POI %s 详情拉取失败，只用列表信息：%s",
                               poi_id, exc)

        existed = await state.scenics.get_scenic(scenic_id)
        await state.scenics.upsert_scenic({
            "scenic_id": scenic_id,
            "scenic_name": row.get("poi_name") or poi_name,
            # 省份来自行政区划表；没同步过就留空，**不从城市名瞎猜**。
            "province": province, "city": row.get("city_name") or city_name,
            "remark": f"从去哪儿导入（POI {poi_id}）",
            "enabled": 1,
        })
        # 采集目标：建完就能直接跑去哪儿点评，不用再手工填 ID
        await state.scenics.upsert_target({
            "scenic_id": scenic_id, "channel": "qunar", "target_type": "poi",
            "target_id": poi_id, "target_name": row.get("poi_name") or poi_name,
            "target_url": f"{catalog.base_url}/{poi_id}",
            "extra": "", "enabled": 1,
        }, keep_url=True)
        await catalog.save_poi_info(row, scenic_id=scenic_id)
        if existed:
            updated += 1
        else:
            created += 1

    message = f"新建景区 {created} 个，更新 {updated} 个"
    if failed:
        message += f"，失败 {len(failed)} 个"
    return ok({"created": created, "updated": updated, "failed": failed}, message)


# ---------------- 行政区划 ----------------
@router.get("/provinces")
async def provinces(state: AppState = Depends(get_state)):
    """省份 → 城市。空列表表示还没同步过行政区划，前端退回平铺城市列表。"""
    return ok(await _catalog(state).provinces())


@router.post("/regions/sync")
async def sync_regions(payload: Dict[str, Any] = Body(default={}),
                       state: AppState = Depends(get_state)):
    """同步一次省市区县。四千多行，几秒钟，平时不用管；去哪儿加了新城市再点。"""
    url = str((payload or {}).get("region_url") or "")
    result = await _catalog(state).sync_regions(url)
    return ok(result,
              f"行政区划 {result['saved']} 行，其中 {result['matched']} 行接上了去哪儿城市")
