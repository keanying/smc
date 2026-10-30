# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""版本信息与景区盒子的分配状态（见 app/edition.py、app/box/）。

    GET  /api/edition                   不用登录：总部版还是景区版（页面据此换布局、藏菜单）
    GET  /api/edition/assignment        景区版：分到了哪些景区、和主同步得怎么样
    POST /api/edition/sync              景区版：马上从主拉一次分配（管理员）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import edition as E
from ..auth import current_user
from ..box.sync import get_box_client, get_box_sync
from .common import bad_request, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/edition", tags=["版本"])


@router.get("")
async def edition_info(state: AppState = Depends(get_state)):
    return ok(E.info(state.config))


def _sync_status():
    client, sync = get_box_client(), get_box_sync()
    return {
        "master_configured": bool(client and client.configured),
        **(sync.status if sync else {"ok": None, "last_ok_at": "", "last_error": "", "box_id": ""}),
    }


@router.get("/assignment")
async def get_assignment(state: AppState = Depends(get_state)):
    record = await E.assignment(state.settings) or {}
    record.pop("digest", None)
    options = await state.scenics.list_scenic_options(enabled_only=True)
    return ok({**record, "box_name": E.info(state.config)["box_name"],
               "scenics": options, "sync": _sync_status()})


@router.post("/sync")
async def sync_now(state: AppState = Depends(get_state)):
    sync = get_box_sync()
    if not E.is_scenic() or sync is None:
        raise bad_request("只有景区盒子需要同步分配")
    user = current_user()
    if user is not None and user.get("role") != "admin":
        raise bad_request("只有管理员能手动同步")
    status = await sync.sync_once()
    if not status.get("ok"):
        raise bad_request(f"同步失败：{status.get('last_error')}")
    return ok(_sync_status(), "已和主同步")
