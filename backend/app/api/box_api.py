# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主这边：景区盒子调的接口（要带盒子令牌，见 box/gateway.py）。

    GET  /api/box/assignment          分到的景区（连同关键字、词表、采集目标）
    POST /api/box/data/{method}       上报 / 采集时要查的数据，method 见 _METHODS

上报的数据必须属于这个盒子分到的景区，否则整批拒绝；写进去的每一行记上盒子编号
（source_box，主的页面不显示，只用于追查）。评论写完推给主的标注引擎（边采边标）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import edition as E
from ..box.codec import decode, encode, to_dataclass
from ..box.repo import get_box_repo
from ..collectors.base import AuthorItem, CommentItem, WorkItem
from ..core.logging import get_logger
from .common import bad_request, ok
from .deps import AppState, get_state

logger = get_logger(__name__)
router = APIRouter(prefix="/api/box", tags=["景区盒子"])

#: 每次上报最多多少条：盒子按批（几百条）报，太大的请求多半是出错了
MAX_ITEMS = 5000


def _box(request: Request) -> Dict[str, Any]:
    box = (request.scope.get("state") or {}).get("box")
    if not box:
        raise HTTPException(status_code=401, detail="要带盒子令牌")
    return box


def _check_scenic(box: Dict[str, Any], scenic_id: str) -> None:
    if (scenic_id or "") not in box["scenic_ids"]:
        raise HTTPException(status_code=403,
                            detail=f"景区 {scenic_id or '（空）'} 没有分给这个盒子，数据不收")


def _items(box, payload: Dict[str, Any], cls) -> List[Any]:
    raw = payload.get("items") or []
    if not isinstance(raw, list):
        raise bad_request("items 应该是数组")
    if len(raw) > MAX_ITEMS:
        raise bad_request(f"一次最多上报 {MAX_ITEMS} 条")
    items = [to_dataclass(cls, r) for r in raw]
    if cls is not AuthorItem:
        for item in items:
            _check_scenic(box, item.scenic_id)
            item.source_box = box["box_id"]
    return items


@router.get("/assignment")
async def assignment(request: Request, state: AppState = Depends(get_state)):
    box = _box(request)
    bundle = await E.export_bundle(state.scenics, box["scenic_ids"], box_name=box["box_name"],
                                   exported_by="主", strict=False)
    return ok({"box_id": box["box_id"], "box_name": box["box_name"], "bundle": bundle})


@router.post("/data/{method}")
async def data_call(method: str, request: Request, payload: Dict[str, Any] = Body(...),
                    state: AppState = Depends(get_state)):
    box = _box(request)
    repo = get_box_repo()
    data = state.data
    if method == "save_works":
        items = _items(box, payload, WorkItem)
        new, updated = await data.save_works(items)
        await repo.count_upload(box["box_id"], works=new)
        result: Any = [new, updated]
    elif method == "merge_work_hits":
        result = await data.merge_work_hits(_items(box, payload, WorkItem))
    elif method == "save_comments":
        items = _items(box, payload, CommentItem)
        new, updated = await data.save_comments(items, skip_existing=bool(payload.get("skip_existing", True)))
        await repo.count_upload(box["box_id"], comments=new)
        # 边采边标：写库之后推给主的标注引擎。标注出错不影响上报
        try:
            from ..scheduler.runner import submit_for_labeling
            await submit_for_labeling(items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[盒子 %s] 推送标注失败（数据已收下）：%s", box["box_id"], exc)
        result = [new, updated]
    elif method == "save_authors":
        result = await data.save_authors(_items(box, payload, AuthorItem))
    elif method == "work_ids_collected_since":
        _check_scenic(box, payload.get("scenic_id"))
        since = decode(payload.get("since"))
        if not isinstance(since, datetime):
            raise bad_request("since 应该是时间")
        result = await data.work_ids_collected_since(payload["scenic_id"], payload.get("channel") or "", since)
    elif method == "deleted_work_keys":
        _check_scenic(box, payload.get("scenic_id"))
        result = await data.deleted_work_keys(payload["scenic_id"], payload.get("channel") or "")
    else:
        raise bad_request(f"不认识的方法：{method}")
    if method in ("save_works", "save_comments", "save_authors", "merge_work_hits"):
        data.invalidate_overview_cache()
    return ok(encode(result))
