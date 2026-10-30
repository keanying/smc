# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""主这边的「盒子管理」：登记景区盒子、分配景区、发令牌（只有管理员能用，见 auth/roles.py）。

令牌只在新建和重置时返回一次明文，之后谁都看不到——丢了就重置。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, Depends

from ..box.repo import BoxRepository, get_box_repo
from .common import bad_request, not_found, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/boxes", tags=["盒子管理"])

#: 多久没连上主算离线（盒子每分钟拉一次分配）
ONLINE_SECONDS = 180


def _repo() -> BoxRepository:
    repo = get_box_repo()
    if repo is None:
        raise bad_request("服务还没启动完")
    return repo


async def _clean_scenics(state: AppState, scenic_ids: List[str]) -> List[str]:
    ids = list(dict.fromkeys(s.strip() for s in scenic_ids or [] if s and s.strip()))
    for scenic_id in ids:
        if not await state.scenics.get_scenic(scenic_id):
            raise bad_request(f"景区不存在：{scenic_id}")
    return ids


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise bad_request("给盒子起个名字，如「A景区 1号盒子」")
    if len(name) > 100:
        raise bad_request("名字太长了（最多 100 个字）")
    return name


@router.get("")
async def list_boxes(state: AppState = Depends(get_state)):
    boxes = await _repo().list()
    names = {s["scenic_id"]: s["scenic_name"]
             for s in await state.scenics.list_scenic_options(enabled_only=False)}
    online_ids = {r["box_id"] for r in await state.db.fetch_all(
        "SELECT box_id FROM `src_opinion_box` WHERE last_seen_at > NOW() - INTERVAL %s SECOND",
        [ONLINE_SECONDS])}
    for box in boxes:
        box["online"] = box["box_id"] in online_ids
        box["scenics"] = [{"scenic_id": i, "scenic_name": names.get(i, i)} for i in box["scenic_ids"]]
    return ok(boxes)


@router.post("")
async def create_box(box_name: str = Body(..., embed=True), scenic_ids: List[str] = Body([], embed=True),
                     state: AppState = Depends(get_state)):
    box, token = await _repo().create(_clean_name(box_name), await _clean_scenics(state, scenic_ids))
    return ok({"box": box, "token": token}, "盒子已登记。令牌只显示这一次，请马上配到盒子上")


@router.put("/{box_id}")
async def update_box(box_id: str, box_name: Optional[str] = Body(None, embed=True),
                     scenic_ids: Optional[List[str]] = Body(None, embed=True),
                     enabled: Optional[bool] = Body(None, embed=True),
                     state: AppState = Depends(get_state)):
    repo = _repo()
    if not await repo.get(box_id):
        raise not_found("盒子不存在")
    fields = {}
    if box_name is not None:
        fields["box_name"] = _clean_name(box_name)
    if scenic_ids is not None:
        fields["scenic_ids"] = await _clean_scenics(state, scenic_ids)
    if enabled is not None:
        fields["enabled"] = enabled
    await repo.update(box_id, fields)
    return ok(await repo.get(box_id), "已保存，盒子一分钟内同步")


@router.post("/{box_id}/token")
async def reset_token(box_id: str):
    repo = _repo()
    if not await repo.get(box_id):
        raise not_found("盒子不存在")
    return ok({"token": await repo.reset_token(box_id)}, "令牌已重置，旧令牌立刻失效")


@router.delete("/{box_id}")
async def delete_box(box_id: str):
    await _repo().delete(box_id)
    return ok(None, "已删除。它上报过的数据还在")
