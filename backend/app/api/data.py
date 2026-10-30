# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""数据中心 API：景区 x 平台概览、作品列表、评论树、创作者、CSV 导出。"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from .common import bad_request, not_found, ok
from .deps import AppState, get_state
from ..core.constants import CHANNELS_POI_BASED
from ..core.scope import in_scope
from ..db import tables

router = APIRouter(prefix="/api/data", tags=["数据中心"])


@router.get("/trend")
async def trend(days: int = 14, state: AppState = Depends(get_state)):
    """最近几天每天采到的作品数、评论数（按采集时间），概览页趋势图。"""
    return ok(await state.data.daily_trend(days))


@router.get("/overview")
async def overview(state: AppState = Depends(get_state)):
    """按景区 + 平台聚合的作品数/评论数，数据中心首屏。"""
    rows = await state.data.scenic_overview()
    grouped: dict = {}
    for row in rows:
        if not in_scope(row["scenic_id"]):      # 盒子来查：只看它分到的景区
            continue
        bucket = grouped.setdefault(row["scenic_id"], {
            "scenic_id": row["scenic_id"],
            "scenic_name": row["scenic_name"],
            "channels": [],
            "total_works": 0,
            "total_comments": 0,
        })
        bucket["channels"].append({
            "channel": row["channel"],
            "work_cnt": int(row["work_cnt"]),
            "comment_cnt": int(row["comment_cnt"]),
            "last_crawl": row["last_crawl"],
        })
        bucket["total_works"] += int(row["work_cnt"])
        bucket["total_comments"] += int(row["comment_cnt"])
    return ok(list(grouped.values()))


@router.get("/works")
async def list_works(
    scenic_id: str = "", channel: str = "", keyword: str = "", author_id: str = "",
    start_time: str = "", end_time: str = "", include_synthetic: bool = True,
    page: int = 1, page_size: int = 20,
    order_by: str = "publish_time", order: str = "desc",
    state: AppState = Depends(get_state),
):
    return ok(await state.data.list_works(
        scenic_id=scenic_id or None, channel=channel or None, keyword=keyword or None,
        author_id=author_id or None, start_time=start_time or None, end_time=end_time or None,
        include_synthetic=include_synthetic, page=page, page_size=page_size,
        order_by=order_by, order=order,
    ))


@router.delete("/works/{work_pk}")
async def delete_work(work_pk: int, state: AppState = Depends(get_state)):
    """删除一条作品，连同它下面的全部评论。

    复核觉得不相关就删。删掉的会记下来，之后的采集再搜到它直接跳过，
    不会被下一轮又加回来。携程/同程/去哪儿的"作品"是景区点评的挂载点，
    删它等于删掉整个景区的点评，不允许从这里删。
    """
    row = await state.db.fetch_one(
        f"SELECT channel, scenic_id FROM `{tables.WORKS}` WHERE id = %s", [work_pk])
    if row is None or not in_scope(row["scenic_id"]):
        raise not_found("作品不存在（可能已经删过了）")
    if row["channel"] in CHANNELS_POI_BASED:
        raise bad_request("携程/同程/去哪儿的这一条是景区点评的集合，不能整条删除")
    result = await state.data.delete_work(work_pk)
    if result is None:
        raise not_found("作品不存在（可能已经删过了）")
    return ok(result, f"已删除作品及其 {result['comments_deleted']} 条评论")


@router.get("/comments")
async def list_comments(
    channel: str = Query(..., description="平台"),
    work_id: str = Query(..., description="作品ID"),
    scenic_id: str = "",
    parent_id: Optional[str] = Query(
        None, description="传了就取该评论的直接子评论；不传取一级评论"
    ),
    level: str = "",
    page: int = 1, page_size: int = 20,
    state: AppState = Depends(get_state),
):
    """评论按层级懒加载：前端点"展开回复"就带上父评论ID再调一次。"""
    return ok(await state.data.list_comments(
        channel=channel, work_id=work_id, scenic_id=scenic_id or None,
        parent_id=parent_id, level=level or None, page=page, page_size=page_size,
    ))


@router.get("/comments/thread")
async def comment_thread(
    channel: str, work_id: str, root_comment_id: str,
    state: AppState = Depends(get_state),
):
    """一次取出整条会话（一级 + 所有子级），用于"全部展开"。"""
    rows = await state.data.comment_thread(channel, work_id, root_comment_id)
    return ok(_build_tree(rows))


def _build_tree(rows: list) -> list:
    """按 comment_parent_id 组装成嵌套结构，前端直接渲染。"""
    by_id = {row["comment_id"]: {**row, "children": []} for row in rows}
    roots = []
    for row in rows:
        node = by_id[row["comment_id"]]
        parent_id = row.get("comment_parent_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/authors")
async def list_authors(
    channel: str = "", scenic_id: str = "", page: int = 1, page_size: int = 20,
    state: AppState = Depends(get_state),
):
    return ok(await state.data.list_authors(
        channel=channel or None, scenic_id=scenic_id or None,
        page=page, page_size=page_size,
    ))


@router.get("/export/{kind}")
async def export_csv(
    kind: str,
    scenic_id: str = "", channel: str = "", task_id: str = "", work_id: str = "",
    start_time: str = "", end_time: str = "", english_headers: bool = False,
    state: AppState = Depends(get_state),
):
    """导出 CSV。kind = works | comments。

    流式返回，几十万行也不会把内存吃满；
    默认 utf-8-sig 编码，Excel 直接双击打开不乱码。
    """
    table = tables.WORKS if kind == "works" else tables.COMMENTS
    filters = {
        "scenic_id": scenic_id, "channel": channel, "task_id": task_id,
        "work_id": work_id, "start_time": start_time, "end_time": end_time,
    }
    from ..utils.exporter import build_filename
    filename = build_filename(table, filters)

    return StreamingResponse(
        state.exporter.stream(table, filters, use_chinese_headers=not english_headers),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )
