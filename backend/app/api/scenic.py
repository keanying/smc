"""景区、关键字、平台采集目标 API。"""
from __future__ import annotations

import csv
import io
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile

from ..core.constants import ALL_CHANNELS, CHANNEL_LABELS
from .common import bad_request, not_found, ok
from .deps import AppState, get_state
from .schemas import (FilterWordsIn, KeywordsIn, ScenicBulkIn,
                      ScenicIn, TargetIn)

router = APIRouter(prefix="/api/scenics", tags=["景区"])


@router.get("")
async def list_scenics(
    keyword: str = "", enabled_only: bool = False,
    page: int = 1, page_size: int = 50,
    state: AppState = Depends(get_state),
):
    return ok(await state.scenics.list_scenics(
        keyword=keyword, enabled_only=enabled_only, page=page, page_size=page_size
    ))


@router.get("/options")
async def list_scenic_options(
    enabled_only: bool = True, state: AppState = Depends(get_state),
):
    """下拉框用的精简景区列表（只有 scenic_id / scenic_name）。

    ⚠️ 必须放在 /{scenic_id} 之前——FastAPI 按声明顺序匹配，
    放后面的话 "options" 会被当成一个景区 ID。
    """
    return ok(await state.scenics.list_scenic_options(enabled_only=enabled_only))


@router.get("/channels")
async def list_channels():
    """给前端下拉框用：平台标识与中文名。"""
    return ok([
        {"value": channel, "label": CHANNEL_LABELS[channel]} for channel in ALL_CHANNELS
    ])


@router.get("/{scenic_id}")
async def get_scenic(scenic_id: str, state: AppState = Depends(get_state)):
    scenic = await state.scenics.get_scenic(scenic_id)
    if not scenic:
        raise not_found(f"景区不存在：{scenic_id}")
    scenic["keywords"] = await state.scenics.list_keywords(scenic_id)
    scenic["filter_words"] = await state.scenics.list_filter_words(scenic_id)
    scenic["targets"] = await state.scenics.list_targets(scenic_id)
    return ok(scenic)


@router.post("")
async def upsert_scenic(payload: ScenicIn, state: AppState = Depends(get_state)):
    await state.scenics.upsert_scenic(payload.model_dump())
    return ok(message="保存成功")


@router.post("/bulk")
async def bulk_upsert(payload: ScenicBulkIn, state: AppState = Depends(get_state)):
    result = await state.scenics.bulk_import_scenics(
        [item.model_dump() for item in payload.items]
    )
    return ok(result, f"导入完成：新增 {result['created']} 个，更新 {result['updated']} 个")


@router.post("/import-csv")
async def import_csv(
    file: UploadFile = File(..., description="至少包含 scenic_id、scenic_name 两列"),
    state: AppState = Depends(get_state),
):
    """从 CSV 批量导入景区。列名支持中英文两种写法。"""
    raw = await file.read()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise bad_request("无法识别文件编码，请另存为 UTF-8 或 GBK 的 CSV")

    reader = csv.DictReader(io.StringIO(text))
    alias = {
        "scenic_id": ["scenic_id", "景区ID", "景区id", "id"],
        "scenic_name": ["scenic_name", "景区名称", "景区名字", "name"],
        "province": ["province", "省份"],
        "city": ["city", "城市"],
        "remark": ["remark", "备注"],
    }

    def _pick(row: dict, field: str) -> Optional[str]:
        for key in alias[field]:
            if key in row and row[key] not in (None, ""):
                return str(row[key]).strip()
        return None

    rows = []
    errors = []
    for line_number, row in enumerate(reader, start=2):
        scenic_id = _pick(row, "scenic_id")
        scenic_name = _pick(row, "scenic_name")
        if not scenic_id or not scenic_name:
            errors.append(f"第 {line_number} 行缺少景区ID或景区名称")
            continue
        rows.append({
            "scenic_id": scenic_id, "scenic_name": scenic_name,
            "province": _pick(row, "province"), "city": _pick(row, "city"),
            "remark": _pick(row, "remark"),
        })

    if not rows:
        raise bad_request(
            "没有解析出有效数据。表头至少要有 scenic_id、scenic_name（或 景区ID、景区名称）"
            + ("；问题：" + "；".join(errors[:5]) if errors else "")
        )

    result = await state.scenics.bulk_import_scenics(rows)
    return ok(
        {**result, "errors": errors[:20]},
        f"导入完成：新增 {result['created']} 个，更新 {result['updated']} 个"
        + (f"，跳过 {len(errors)} 行" if errors else ""),
    )


@router.delete("/{scenic_id}")
async def delete_scenic(scenic_id: str, state: AppState = Depends(get_state)):
    await state.scenics.delete_scenic(scenic_id)
    return ok(message="已删除景区及其关键字、采集目标；已采集的数据保留")


# ---------------- 关键字 ----------------

@router.get("/{scenic_id}/keywords")
async def list_keywords(
    scenic_id: str, enabled_only: bool = False, limit: int = 0,
    state: AppState = Depends(get_state),
):
    return ok(await state.scenics.list_keywords(
        scenic_id, enabled_only=enabled_only, limit=limit
    ))


@router.post("/{scenic_id}/keywords")
async def add_keywords(
    scenic_id: str, payload: KeywordsIn, state: AppState = Depends(get_state)
):
    if not await state.scenics.get_scenic(scenic_id):
        raise not_found(f"景区不存在：{scenic_id}")
    result = await state.scenics.add_keywords(scenic_id, payload.keywords)
    return ok(result, f"新增 {result['added']} 个关键字，跳过重复 {result['skipped']} 个")


@router.delete("/keywords/{keyword_id}")
async def delete_keyword(keyword_id: int, state: AppState = Depends(get_state)):
    await state.scenics.delete_keyword(keyword_id)
    return ok(message="已删除")


@router.put("/keywords/{keyword_id}/enabled")
async def toggle_keyword(
    keyword_id: int, enabled: bool = Query(...), state: AppState = Depends(get_state)
):
    await state.scenics.set_keyword_enabled(keyword_id, enabled)
    return ok(message="已更新")


# ---------------- 附关键字 / 过滤关键字 ----------------
@router.get("/{scenic_id}/filter-words")
async def list_filter_words(
    scenic_id: str, kind: str = "", enabled_only: bool = False,
    state: AppState = Depends(get_state),
):
    """kind 留空返回两类；aux=附关键字，exclude=过滤关键字。"""
    try:
        return ok(await state.scenics.list_filter_words(
            scenic_id, kind, enabled_only=enabled_only))
    except ValueError as exc:
        raise bad_request(str(exc))


@router.post("/{scenic_id}/filter-words")
async def add_filter_words(
    scenic_id: str, payload: FilterWordsIn, state: AppState = Depends(get_state)
):
    if not await state.scenics.get_scenic(scenic_id):
        raise not_found(f"景区不存在：{scenic_id}")
    try:
        result = await state.scenics.add_filter_words(
            scenic_id, payload.kind, payload.words)
    except ValueError as exc:
        raise bad_request(str(exc))
    label = "附关键字" if payload.kind == "aux" else "过滤关键字"
    message = f"新增 {result['added']} 个{label}，跳过重复 {result['skipped']} 个"
    if result.get("dropped"):
        # 超上限必须说出来。安静截断的现象是"我贴了 250 个，后面那些怎么不生效"
        message += (f"；超出上限丢弃 {result['dropped']} 个"
                    f"（{label}最多 {state.scenics.max_words_of(payload.kind)} 个）")
    return ok(result, message)


@router.delete("/filter-words/{word_id}")
async def delete_filter_word(word_id: int, state: AppState = Depends(get_state)):
    await state.scenics.delete_filter_word(word_id)
    return ok(message="已删除")


@router.put("/filter-words/{word_id}/enabled")
async def toggle_filter_word(
    word_id: int, enabled: bool = Query(...), state: AppState = Depends(get_state)
):
    await state.scenics.set_filter_word_enabled(word_id, enabled)
    return ok(message="已更新")


# ---------------- 平台采集目标 ----------------

@router.get("/{scenic_id}/targets")
async def list_targets(
    scenic_id: str, channel: str = "", target_type: str = "",
    state: AppState = Depends(get_state),
):
    return ok(await state.scenics.list_targets(
        scenic_id, channel=channel, target_type=target_type
    ))


@router.post("/{scenic_id}/targets")
async def upsert_target(
    scenic_id: str, payload: TargetIn, state: AppState = Depends(get_state)
):
    if not await state.scenics.get_scenic(scenic_id):
        raise not_found(f"景区不存在：{scenic_id}")
    data = payload.model_dump()
    data["scenic_id"] = scenic_id
    await state.scenics.upsert_target(data)
    return ok(message="保存成功")


@router.delete("/targets/{target_id}")
async def delete_target(target_id: int, state: AppState = Depends(get_state)):
    await state.scenics.delete_target(target_id)
    return ok(message="已删除")
