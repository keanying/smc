"""景区档案接口：三个渠道（同程 / 携程 / 去哪儿）的景区资料库。

和 `api/qunar.py` 的分工：那一组是去哪儿**专用**的目录浏览（按城市翻页、
选中导入），这一组是**三渠道统一**的档案库——采一次落库，之后所有操作
都在库里做，不用每次都去打平台的接口。

    GET  /api/archive/channels                渠道清单（带"有没有详情页"）
    GET  /api/archive/regions                 某渠道的可采区域
    POST /api/archive/regions/refresh         重新探测区域清单
    POST /api/archive/regions/add             手工加一条区域（平台改版时的兜底）
    DELETE /api/archive/regions               删一条区域
    POST /api/archive/collect                 开始采集（后台作业）
    GET  /api/archive/jobs                    作业列表
    GET  /api/archive/jobs/{job_id}           单个作业进度
    POST /api/archive/jobs/{job_id}/cancel    停止
    GET  /api/archive/list                    档案列表（筛选 + 分页；scenic_id 可查某景区的全部档案）
    GET  /api/archive/provinces               已入库档案里有哪些省
    GET  /api/archive/stats                   每个渠道各有多少条
    GET  /api/archive/{channel}/{poi_id}      单条详情
    POST /api/archive/{channel}/{poi_id}/refresh   单条重新采集详情
    POST /api/archive/import                  建为景区 + 建采集目标
    POST /api/archive/attach                  只把 POI 挂到已有景区上当采集目标
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, Query

from ..core.logging import get_logger
from ..services import archive as archive_sources
from ..services.archive_manager import get_manager
from ..services.archive_store import ArchiveStore
from .common import bad_request, ok
from .deps import AppState, get_state

logger = get_logger(__name__)
router = APIRouter(prefix="/api/archive", tags=["景区档案"])


def _manager(state: AppState):
    return get_manager(state.config, state.proxy_manager, state.db)


def _store(state: AppState) -> ArchiveStore:
    return ArchiveStore(state.db)


@router.get("/channels")
async def channels():
    return ok(archive_sources.available())


# ---------------- 区域 ----------------
@router.get("/regions")
async def regions(channel: str = Query(...), state: AppState = Depends(get_state)):
    return ok(await _store(state).list_regions(channel))


@router.post("/regions/refresh")
async def refresh_regions(payload: Dict[str, Any] = Body(...),
                          state: AppState = Depends(get_state)):
    channel = str(payload.get("channel") or "")
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    try:
        result = await _manager(state).refresh_regions(channel)
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc)) from exc
    return ok(result, f"区域清单 {result['saved']} 条")


@router.post("/regions/add")
async def add_region(payload: Dict[str, Any] = Body(...),
                     state: AppState = Depends(get_state)):
    channel = str(payload.get("channel") or "")
    region_id = str(payload.get("region_id") or "").strip()
    region_name = str(payload.get("region_name") or "").strip()
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    if not region_id or not region_name:
        raise bad_request("区域编号和名称都要填")
    await _manager(state).add_region(
        channel, region_id, region_name,
        str(payload.get("level") or "province"))
    return ok(None, "已添加")


@router.delete("/regions")
async def delete_region(channel: str = Query(...), region_id: str = Query(...),
                        state: AppState = Depends(get_state)):
    await _store(state).delete_region(channel, region_id)
    return ok(None, "已删除")


# ---------------- 采集作业 ----------------
@router.post("/collect")
async def collect(payload: Dict[str, Any] = Body(...),
                  state: AppState = Depends(get_state)):
    channel = str(payload.get("channel") or "")
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    region_ids = [str(x) for x in (payload.get("region_ids") or []) if str(x).strip()]
    if not region_ids:
        raise bad_request("没有选择任何区域")

    known = {r["region_id"]: r for r in await _store(state).list_regions(channel)}
    regions = [{"region_id": rid,
                "region_name": (known.get(rid) or {}).get("region_name") or rid,
                "level": (known.get(rid) or {}).get("level") or "province"}
               for rid in region_ids]
    try:
        job = _manager(state).start(
            channel, regions,
            with_detail=bool(payload.get("with_detail")),
            limit=int(payload.get("limit") or 0))
    except RuntimeError as exc:
        raise bad_request(str(exc)) from exc
    return ok(job.to_dict(), f"已开始采集 {len(regions)} 个区域")


@router.get("/jobs")
async def jobs(state: AppState = Depends(get_state)):
    return ok(_manager(state).jobs())


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, state: AppState = Depends(get_state)):
    job = _manager(state).job(job_id)
    if job is None:
        raise bad_request("没有这个采集作业（服务重启后进度不保留）")
    return ok(job.to_dict())


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, state: AppState = Depends(get_state)):
    if not _manager(state).cancel(job_id):
        raise bad_request("这个作业已经不在跑了")
    return ok(None, "已请求停止")


# ---------------- 档案读 ----------------
@router.get("/list")
async def list_archive(channel: str = "", province: str = "", city: str = "",
                       keyword: str = "", linked: str = "", level: str = "",
                       scenic_id: str = "", page: int = 1, page_size: int = 20,
                       state: AppState = Depends(get_state)):
    return ok(await _store(state).search(
        channel=channel, province=province, city=city, keyword=keyword,
        linked=linked, level=level, scenic_id=scenic_id,
        page=page, page_size=page_size))


@router.get("/provinces")
async def provinces(channel: str = "", state: AppState = Depends(get_state)):
    return ok(await _store(state).provinces(channel))


@router.get("/stats")
async def stats(state: AppState = Depends(get_state)):
    return ok(await _store(state).stats())


# ---------------- 建景区 / 挂目标 ----------------
def _scenic_id_for(channel: str, poi_id: str, prefix: str = "") -> str:
    """景区ID = 前缀 + POI ID。

    ⚠️ 用平台 ID 而不是自增号：同一个景区重复导入会命中同一个 ID
    直接更新，不会建出两条内容一样、ID 不同的景区。这个系统里
    景区ID是所有数据的归属键，重了之后数据会被劈成两半。
    """
    default = {"tongcheng": "TC", "ctrip": "CT", "qunar": "QN"}
    head = (prefix or default.get(channel) or channel[:2].upper()).strip()
    return f"{head}{poi_id}"


@router.post("/import")
async def import_archive(payload: Dict[str, Any] = Body(...),
                         state: AppState = Depends(get_state)):
    """把勾选的档案建成本系统的景区，并把该渠道的 POI 目标填好。"""
    channel = str(payload.get("channel") or "")
    poi_ids = [str(x) for x in (payload.get("poi_ids") or []) if str(x).strip()]
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    if not poi_ids:
        raise bad_request("没有勾选任何景区")
    prefix = str(payload.get("scenic_prefix") or "").strip()

    store = _store(state)
    created, updated, failed = 0, 0, []
    for poi_id in poi_ids:
        row = await store.get(channel, poi_id)
        if not row:
            failed.append({"poi_id": poi_id, "error": "档案里没有这条"})
            continue
        scenic_id = _scenic_id_for(channel, poi_id, prefix)
        existed = await state.scenics.get_scenic(scenic_id)
        await state.scenics.upsert_scenic({
            "scenic_id": scenic_id,
            "scenic_name": row.get("poi_name") or poi_id,
            "province": row.get("province") or "",
            "city": row.get("city_name") or "",
            "remark": f"从{channel}档案导入（POI {poi_id}）",
            "enabled": 1,
        })
        await state.scenics.upsert_target({
            "scenic_id": scenic_id, "channel": channel, "target_type": "poi",
            "target_id": poi_id, "target_name": row.get("poi_name") or poi_id,
            "target_url": row.get("source_url") or "", "extra": "", "enabled": 1,
        }, keep_url=True)
        await store.link_scenic(channel, poi_id, scenic_id)
        if existed:
            updated += 1
        else:
            created += 1

    message = f"新建景区 {created} 个，更新 {updated} 个"
    if failed:
        message += f"，失败 {len(failed)} 个"
    return ok({"created": created, "updated": updated, "failed": failed}, message)


@router.post("/attach")
async def attach(payload: Dict[str, Any] = Body(...),
                 state: AppState = Depends(get_state)):
    """把一条档案挂到**已有景区**上当采集目标，不新建景区。

    和 import 的区别：一个景区在三个平台上各有一个 POI，用这个接口
    可以把三条都挂到同一个景区下，数据归在一起；走 import 的话
    会建出三个 ID 不同的景区，同一个景区的数据被劈成三份。
    """
    channel = str(payload.get("channel") or "")
    poi_id = str(payload.get("poi_id") or "").strip()
    scenic_id = str(payload.get("scenic_id") or "").strip()
    if not (channel and poi_id and scenic_id):
        raise bad_request("渠道、POI、景区都要给")
    scenic = await state.scenics.get_scenic(scenic_id)
    if not scenic:
        raise bad_request(f"景区 {scenic_id} 不存在")
    store = _store(state)
    row = await store.get(channel, poi_id)
    if not row:
        raise bad_request("档案里没有这条")

    await state.scenics.upsert_target({
        "scenic_id": scenic_id, "channel": channel, "target_type": "poi",
        "target_id": poi_id, "target_name": row.get("poi_name") or poi_id,
        "target_url": row.get("source_url") or "", "extra": "", "enabled": 1,
    }, keep_url=True)
    # 档案的 scenic_id 只在**还没关联**时写：一条档案可能先被建成过景区，
    # 再挂到别的景区上；直接覆盖会把第一次的关联悄悄改掉。
    if not (row.get("scenic_id") or "").strip():
        await store.link_scenic(channel, poi_id, scenic_id)
    return ok(None, f"已挂到景区 {scenic.get('scenic_name') or scenic_id}")


# ---------------- 单条（放最后，避免 /list 之类被当成 channel 吃掉） ----------------
@router.get("/{channel}/{poi_id}")
async def archive_detail(channel: str, poi_id: str,
                         state: AppState = Depends(get_state)):
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    return ok(await _store(state).get(channel, poi_id))


@router.post("/{channel}/{poi_id}/refresh")
async def refresh_detail(channel: str, poi_id: str,
                         state: AppState = Depends(get_state)):
    if channel not in archive_sources.ARCHIVE_CHANNELS:
        raise bad_request(f"不认识的渠道：{channel}")
    try:
        row = await _manager(state).refresh_one(channel, poi_id)
    except Exception as exc:  # noqa: BLE001
        raise bad_request(f"重新采集失败：{exc}") from exc
    return ok(row, "已重新采集")
